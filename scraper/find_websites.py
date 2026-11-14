"""Find the real website for churches that Kyrktorget (and OpenStreetMap) has no website for.

Strategies, cheapest first. A candidate is only accepted after its page has been fetched and it
mentions the church (a distinctive word from the church's name, or for Svenska kyrkan the parish
page itself):

  1. Own e-mail domain      info@sabykyrkan.se            -> https://sabykyrkan.se
  2. Svenska kyrkan         vallentuna.forsamling@svenskakyrkan.se or "Vallentuna församling"
                                                          -> https://www.svenskakyrkan.se/vallentuna
  3. Web search (optional)  needs a Brave Search API key: export BRAVE_API_KEY=...
                            (https://brave.com/search/api/ - about $5 per 1,000 searches, with
                            $5 of free credit each month; a card is needed to activate)

Usage:
    python scraper/find_websites.py --db data/kyrktorget.db --max 50     # test
    python scraper/find_websites.py --db data/kyrktorget.db              # all
then:
    python scraper/normalize.py --db data/kyrktorget.db
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crawl_websites import USER_AGENT, Polite  # noqa: E402
from normalize import name_tokens  # noqa: E402

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

# Mail providers and national umbrella domains: an address there says nothing about the church's site
GENERIC_MAIL = {
    "gmail.com", "hotmail.com", "hotmail.se", "live.se", "live.com", "outlook.com", "outlook.se", "yahoo.se",
    "yahoo.com", "msn.com", "icloud.com", "me.com", "mac.com", "telia.com", "telia.se", "tele2.se", "spray.se",
    "bredband.net", "bredband2.com", "comhem.se", "home.se", "glocalnet.net", "bahnhof.se", "passagen.se",
    "swipnet.se", "tiscali.se", "algonet.se", "kyrktorget.net", "kyrktorget.se", "allt1.se", "allt2.se",
    "katolskakyrkan.se", "adventist.se", "efs.nu", "equmeniakyrkan.nu", "equmeniakyrkan.se", "pingst.se",
    "fralsningsarmen.se", "evangeliskafrikyrkan.se", "efk.se", "lkk.se", "svenskakyrkan.se", "protonmail.com",
    "proton.me", "mail.com", "gmx.com", "gmx.net", "aol.com",
}
SEARCH_SKIP = ("kyrktorget.se", "facebook.com", "instagram.com", "youtube.com", "wikipedia.org", "eniro.se",
               "hitta.se", "allabolag.se", "merinfo.se", "ratsit.se", "google.", "linkedin.com", "proff.se",
               "bolagsfakta.se", "upplysning.se", "foursquare.com", "tripadvisor.", "kyrkokartan.se")
SVK_WORDS = {"forsamling", "forsamlingar", "pastorat", "info", "kansli", "exp", "expedition", "kyrkoherde",
             "kyrkokansli", "kyrkokansliet", "svenska", "kyrkan", "i", "och"}


def ascii_slug(s: str, sep: str = "") -> str:
    s = s.lower()
    for a, b in (("å", "a"), ("ä", "a"), ("ö", "o"), ("é", "e"), ("ü", "u"), ("æ", "a"), ("ø", "o")):
        s = s.replace(a, b)
    return sep.join(re.findall(r"[a-z0-9]+", s))


def svk_candidates(name: str, email: str | None) -> list[str]:
    """Possible svenskakyrkan.se/<slug> pages for a Svenska kyrkan parish."""
    slugs: list[str] = []
    if email and email.lower().endswith("@svenskakyrkan.se"):
        local = email.split("@")[0].lower()
        parts = [p for p in re.split(r"[._-]", local) if p and p not in SVK_WORDS and not p.isdigit()]
        if parts:
            slugs += ["".join(parts), "-".join(parts), parts[0]]
    n = re.sub(r"^svenska kyrkan i\s+", "", name, flags=re.I)
    n = n.split(" - ")[0]
    n = re.sub(r"\b(församling(ar|en)?|pastorat|kyrkliga samfällighet|domkyrkoförsamling)\b", " ", n, flags=re.I)
    base = n.strip(" ,-")
    if base:
        # genitive: "Nordmarkens" -> "Nordmarken", "Östra Ryds" -> "Östra Ryd"
        nominative = re.sub(r"(\w{3,})s$", r"\1", base)
        kind = "pastorat" if re.search(r"pastorat", name, re.I) else "forsamling"
        for b in dict.fromkeys([nominative, base]):
            slugs += [ascii_slug(b), ascii_slug(b, "-")]
        slugs.append(ascii_slug(base) + kind)
        if " och " in base.lower() or "," in base:
            slugs.append(ascii_slug(re.split(r",| och ", base, flags=re.I)[0]))
    out = []
    for s in slugs:
        if s and len(s) > 2 and s not in out:
            out.append(s)
    return [f"https://www.svenskakyrkan.se/{s}" for s in out[:6]]


NOT_SVK = re.compile(
    r"pingst|baptist|missions|katolsk|ortodox|advent|metodist|frikyrk|evangeli|equmenia|\befs\b|\belm\b|vineyard|"
    r"filadelfia|frälsnings|lutherska bekännelse|nyapostolisk|koreansk|eritre|syrisk|armenisk|serbisk|allians|"
    r"betania|elim|smyrna|sion|hope|united|international|nazaren|kristen gemenskap|livets ord|trosgemenskap",
    re.I)


def is_svk_name(name: str, denomination: str | None = None) -> bool:
    """Is this (probably) a Svenska kyrkan parish? Uses Kyrktorget's denomination when known."""
    if denomination:
        return "svenska kyrkan" in denomination.lower()
    n = name.strip()
    if "svenska kyrkan" in n.lower():
        return True
    return bool(re.search(r"församling|pastorat", n, re.I)) and not NOT_SVK.search(n)


def looks_like(name: str, place: dict, html: str, final_url: str) -> bool:
    """Does this page belong to this church? Weighs the church's name against its location.

    Words of the name that are just the town ("Osby" in "Osby Adventistförsamling") count as location,
    not as name. The remaining distinctive words ("adventistförsamling") must ALL be on the page;
    a partial name match is only accepted with strong location evidence (street, postal code, phone).
    """
    soup = BeautifulSoup(html[:600_000], "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    title = (soup.title.get_text(" ") if soup.title else "") + " " + urllib.parse.unquote(final_url)
    raw = " ".join([title, soup.get_text(" ")[:80_000]]).lower()
    text = " " + ascii_slug(raw, " ") + " "
    head = " " + ascii_slug(title, " ") + " " + ascii_slug(urllib.parse.unquote(final_url)) + " "
    digits = re.sub(r"\D", "", raw)

    def forms(t: str) -> list[str]:
        """Swedish word forms: genitive -s, definite -n/-en, and the first part of compounds
        (Alliansförsamling ~ Allianskyrkan ~ allians, Adventkyrkan ~ adventkyrka)."""
        out = {t}
        if t.endswith("s") and len(t) > 4:
            out.add(t[:-1])
        for suf in ("en", "n"):
            if t.endswith(suf) and len(t) > 5:
                out.add(t[: -len(suf)])
        for suf in ("forsamlingen", "forsamlingar", "forsamling", "kyrkan", "kyrka", "foreningen", "forening",
                    "kapellet", "kapell"):
            if t.endswith(suf) and len(t) - len(suf) >= 5:
                out.add(t[: -len(suf)])
        return sorted(out, key=len, reverse=True)

    def on_page(t: str, where: str = text) -> bool:
        return any(f in where for f in forms(t))

    places = set()
    for town in (place.get("city"), place.get("municipality")):
        places |= set(ascii_slug(town or "", " ").split())
    words = [ascii_slug(t) for t in name_tokens(name) if len(t) > 2]
    distinctive = [w for w in words if w and w not in places and w.rstrip("s") not in places]
    name_places = [w for w in words if w not in distinctive]

    clues = 0
    if any(p and len(p) > 2 and re.search(rf"\b{re.escape(p)}s?\b|{re.escape(p)}", head + " " + text[:20_000])
           for p in places):
        clues += 1
    street = re.sub(r"\s*\d.*$", "", place.get("address") or "").strip()
    if len(street) > 4 and ascii_slug(street, " ") in text:
        clues += 1
    pc = re.sub(r"\D", "", place.get("postal_code") or "")
    if len(pc) == 5 and re.search(rf"\b{pc[:3]}\s?{pc[3:]}\b", raw):
        clues += 1
    phone = re.sub(r"\D", "", (place.get("phone") or "").split(";")[0])
    if len(phone) >= 7 and (phone in digits or phone.lstrip("0") in digits):
        clues += 2   # a matching phone number is very strong evidence

    svk_page = "svenskakyrkan.se" in final_url
    if svk_page and not is_svk_name(name, place.get("denomination")):
        return False                              # a Baptist/Adventist/... church is never a Svenska kyrkan page
    if svk_page and any(on_page(w, head) for w in words) and clues >= 1:
        return True                               # the parish's own page or its pastorat's page

    if distinctive:
        hits = sum(1 for w in distinctive if on_page(w))
        if hits == len(distinctive):
            return clues >= 1 or len(distinctive) >= 2
        return hits >= 1 and clues >= 3          # name partly differs, but address/phone match
    # The name is only a place ("Mellösa församling"): that place must be the page's subject
    if name_places and any(on_page(w, head) for w in name_places):
        return clues >= 1
    return clues >= 3


async def brave(request, key: str, query: str) -> list[str]:
    r = await request.get("https://api.search.brave.com/res/v1/web/search",
                          params={"q": query, "country": "se", "search_lang": "sv", "count": "8"},
                          headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=30_000)
    if not r.ok:
        raise RuntimeError(f"Brave Search HTTP {r.status}: {(await r.text())[:200]}")
    data = await r.json()
    return [x.get("url") for x in data.get("web", {}).get("results", []) if x.get("url")]


def homepage(url: str) -> str:
    """Shorten a search hit to the site's start page, keeping the parish path on shared platforms."""
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower()
    if host.endswith("svenskakyrkan.se"):
        first = [x for x in p.path.split("/") if x][:1]
        return f"https://www.svenskakyrkan.se/{first[0]}" if first else url
    return f"{p.scheme}://{p.netloc}/"


async def run(db: Path, limit: int, workers: int, delay: float, use_search: bool) -> None:
    con = sqlite3.connect(db)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    tried = {r[0] for r in con.execute("SELECT DISTINCT org_id FROM raw_sites WHERE kind IN ('found','notfound')")}
    rows = [r for r in con.execute(
        "SELECT id, name, email, city, municipality, address, postal_code, phone, denomination FROM organizations "
        "WHERE website IS NULL ORDER BY id").fetchall()
        if r[0] not in tried]
    if limit:
        rows = rows[:limit]
    key = os.environ.get("BRAVE_API_KEY", "").strip() if use_search else ""
    print(f"{len(rows)} churches without a website to look for"
          f" ({'with' if key else 'without'} web search{'' if key else '; set BRAVE_API_KEY to enable'})")
    stats = {"found_email": 0, "found_svk": 0, "found_search": 0, "not_found": 0, "errors": 0}

    async with async_playwright() as pw:
        request = await pw.request.new_context(user_agent=USER_AGENT,
                                               extra_http_headers={"Accept-Language": "sv-SE,sv;q=0.9"})
        polite = Polite(request, delay)
        queue: asyncio.Queue = asyncio.Queue()
        for r in rows:
            queue.put_nowait(r)
        done = {"n": 0}

        async def check(url: str, name: str, place: dict, svk: bool):
            try:
                st, final, html = await polite.get(url)
            except Exception:
                return None
            if st != 200 or not html:
                return None
            if svk:
                want = urllib.parse.urlparse(url).path.rstrip("/").lower()
                got = urllib.parse.urlparse(final).path.rstrip("/").lower()
                if not got.startswith(want):
                    return None   # redirected away (e.g. to a search page): slug does not exist
            return (final, html) if looks_like(name, place, html, final) else None

        def store(oid, kind, url, final, html, how):
            con.execute("INSERT OR REPLACE INTO raw_sites(url,org_id,kind,status,final_url,body,fetched_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (url or f"notfound:{oid}", oid, kind, 200 if final else None, final,
                         (html or "")[:300_000] if final else how,
                         dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
            con.commit()

        async def worker():
            while True:
                try:
                    oid, name, email, city, muni, address, postal_code, phone, denom = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                place = {"city": city, "municipality": muni, "address": address,
                         "postal_code": postal_code, "phone": phone, "denomination": denom}
                city = city or muni
                found = None
                domain = (email or "").split("@")[-1].lower().strip(" .,") if email and "@" in email else ""
                # 1. own e-mail domain
                if domain and domain not in GENERIC_MAIL and "." in domain:
                    for url in (f"https://www.{domain}/", f"https://{domain}/"):
                        res = await check(url, name, place, svk=False)
                        if res:
                            found = (url, *res, "found_email")
                            break
                # 2. Svenska kyrkan parish page
                if not found and (domain == "svenskakyrkan.se" or is_svk_name(name, denom)):
                    for url in svk_candidates(name, email):
                        res = await check(url, name, place, svk=True)
                        if res:
                            found = (url, *res, "found_svk")
                            break
                # 3. web search
                if not found and key:
                    try:
                        hits = await brave(request, key, f"{name} {city or ''}".strip())
                        await asyncio.sleep(1.1)   # free plan: 1 query per second
                    except Exception as exc:
                        print(f"  [search error] {exc}", file=sys.stderr)
                        hits = []
                    seen = set()
                    for hit in hits:
                        if any(s in hit for s in SEARCH_SKIP):
                            continue
                        url = homepage(hit)
                        if url in seen:
                            continue
                        seen.add(url)
                        res = await check(url, name, place, svk="svenskakyrkan.se" in url)
                        if res:
                            found = (url, *res, "found_search")
                            break
                        if len(seen) >= 3:
                            break
                if found:
                    url, final, html, how = found
                    store(oid, "found", url, final, html, how)
                    stats[how] += 1
                else:
                    store(oid, "notfound", None, None, None, "searched" if key else "no-search")
                    stats["not_found"] += 1
                done["n"] += 1
                if done["n"] % 20 == 0 or done["n"] == len(rows):
                    print(f"  [{done['n']}/{len(rows)}] {json.dumps(stats)}")

        await asyncio.gather(*(worker() for _ in range(workers)))
        await request.dispose()
    con.close()
    print("Done:", stats)
    print("Next: python scraper/normalize.py --db", db)


def recheck(db: Path) -> None:
    con = sqlite3.connect(db)
    rows = con.execute("SELECT r.url, r.org_id, r.final_url, r.body, o.name, o.city, o.municipality, o.address,"
                       " o.postal_code, o.phone, o.denomination FROM raw_sites r JOIN organizations o ON o.id = r.org_id"
                       " WHERE r.kind='found'").fetchall()
    dropped = []
    for url, oid, final, body, name, city, muni, address, pc, phone, denom in rows:
        place = {"city": city, "municipality": muni, "address": address, "postal_code": pc, "phone": phone,
                 "denomination": denom}
        if not looks_like(name, place, body or "", final or url):
            dropped.append((name, final))
            con.execute("UPDATE raw_sites SET kind='notfound', final_url=NULL, body='rejected-on-recheck' "
                        "WHERE url=? AND org_id=?", (url, oid))
    con.commit()
    con.close()
    print(f"Rechecked {len(rows)} websites: kept {len(rows) - len(dropped)}, removed {len(dropped)}")
    for n, f in dropped:
        print(f"  removed: {n} -> {f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/kyrktorget.db", type=Path)
    ap.add_argument("--max", default=0, type=int, help="max churches this run (0 = all)")
    ap.add_argument("--workers", default=6, type=int)
    ap.add_argument("--delay", default=1.5, type=float, help="seconds between requests to the same site")
    ap.add_argument("--retry", action="store_true", help="look again for churches not found earlier")
    ap.add_argument("--no-search", action="store_true", help="skip web search even if BRAVE_API_KEY is set")
    ap.add_argument("--recheck", action="store_true",
                    help="re-test websites found earlier against the current rules (no internet needed)")
    a = ap.parse_args()
    if a.recheck:
        recheck(a.db)
        return
    if a.retry:
        con = sqlite3.connect(a.db)
        con.execute("DELETE FROM raw_sites WHERE kind='notfound'")
        con.commit()
        con.close()
    asyncio.run(run(a.db, a.max, a.workers, a.delay, not a.no_search))


if __name__ == "__main__":
    main()
