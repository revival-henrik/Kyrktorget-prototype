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


def is_svk_name(name: str) -> bool:
    return bool(re.search(r"(församling(ar)?|pastorat)$|^svenska kyrkan", name.strip(), re.I)) and not re.search(
        r"pingst|baptist|missions|katolsk|ortodox|advent|metodist|frikyrk|evangeli|equmenia|efs|elm|vineyard|"
        r"filadelfia|frälsnings|lutherska bekännelse|nyapostolisk|koreansk|finsk|eritre|syrisk|armenisk|serbisk",
        name, re.I)


def looks_like(name: str, place: dict, html: str, final_url: str) -> bool:
    """Does this page belong to this church? Weighs the church's name against its location.

    name words:  how many distinctive words of the name appear (Adventkyrkan, Gränna …)
    location:    town/municipality, street name, postal code or phone number on the page
    Accepted when all name words match, or most of them plus at least one location clue.
    """
    soup = BeautifulSoup(html[:600_000], "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    raw = " ".join([soup.title.get_text(" ") if soup.title else "", soup.get_text(" ")[:80_000],
                    urllib.parse.unquote(final_url)]).lower()
    text = ascii_slug(raw, " ")
    digits = re.sub(r"\D", "", raw)

    tokens = [ascii_slug(t) for t in name_tokens(name) if len(t) > 2]
    hits = sum(1 for t in tokens if t and t in text)
    ratio = hits / len(tokens) if tokens else 0.0

    clues = 0
    for town in {place.get("city"), place.get("municipality")} - {None, ""}:
        if ascii_slug(town, " ") in text:
            clues += 1
            break
    street = re.sub(r"\s*\d.*$", "", place.get("address") or "").strip()
    if len(street) > 4 and ascii_slug(street, " ") in text:
        clues += 1
    pc = re.sub(r"\D", "", place.get("postal_code") or "")
    if len(pc) == 5 and re.search(rf"\b{pc[:3]}\s?{pc[3:]}\b", raw):
        clues += 1
    phone = re.sub(r"\D", "", (place.get("phone") or "").split(";")[0])
    if len(phone) >= 7 and (phone in digits or phone.lstrip("0") in digits):
        clues += 2   # a matching phone number is very strong evidence

    if ratio == 1 and (clues or len(tokens) >= 2):
        return True
    if ratio >= 0.5 and clues >= 1:
        return True
    return clues >= 3   # name written differently, but address + phone match


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
        "SELECT id, name, email, city, municipality, address, postal_code, phone FROM organizations "
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
                    oid, name, email, city, muni, address, postal_code, phone = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                place = {"city": city, "municipality": muni, "address": address,
                         "postal_code": postal_code, "phone": phone}
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
                if not found and (domain == "svenskakyrkan.se" or is_svk_name(name)):
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/kyrktorget.db", type=Path)
    ap.add_argument("--max", default=0, type=int, help="max churches this run (0 = all)")
    ap.add_argument("--workers", default=6, type=int)
    ap.add_argument("--delay", default=1.5, type=float, help="seconds between requests to the same site")
    ap.add_argument("--retry", action="store_true", help="look again for churches not found earlier")
    ap.add_argument("--no-search", action="store_true", help="skip web search even if BRAVE_API_KEY is set")
    a = ap.parse_args()
    if a.retry:
        con = sqlite3.connect(a.db)
        con.execute("DELETE FROM raw_sites WHERE kind='notfound'")
        con.commit()
        con.close()
    asyncio.run(run(a.db, a.max, a.workers, a.delay, not a.no_search))


if __name__ == "__main__":
    main()
