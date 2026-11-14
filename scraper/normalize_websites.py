"""Update parishes with information from their own websites (fetched by crawl_websites.py).

What it reads, in order of reliability:
  1. Structured data for search engines (schema.org JSON-LD): phone, e-mail, address,
     coordinates and events.
  2. iCal calendar feeds: events for the coming weeks.
  3. Plain page content: tel:/mailto: links and the page description.

Website information is newer than Kyrktorget's copy, so it wins where both exist.
Staff names and photos are deliberately NOT collected (personal data).

Usage:
    python scraper/normalize_websites.py --db data/kyrktorget.db
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import urllib.parse
from collections import defaultdict
from pathlib import Path

from bs4 import BeautifulSoup

ORG_TYPES = {"organization", "church", "placeofworship", "localbusiness", "place", "religiousorganization",
             "ngo", "civicstructure"}
EVENT_DAYS = 60
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
PHONE_RE = re.compile(r"^\+?[\d][\d \-()]{5,}\d$")


def clean(s) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None


def first(v):
    return v[0] if isinstance(v, list) and v else v


# ------------------------------------------------------------------ JSON-LD
def jsonld_items(soup: BeautifulSoup) -> list[dict]:
    out = []
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            try:  # some sites put several objects or trailing commas in one block
                data = json.loads("[" + re.sub(r",\s*([}\]])", r"\1", raw.rstrip(";")) + "]")
            except ValueError:
                continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                out.append(x)
                for k in ("@graph", "subEvent", "event", "events", "location", "mainEntity"):
                    if k in x:
                        stack.append(x[k])
    return out


def types_of(item: dict) -> set[str]:
    t = item.get("@type", [])
    return {str(x).lower() for x in (t if isinstance(t, list) else [t])}


def org_from_jsonld(items: list[dict]) -> dict:
    d: dict = {}
    for it in items:
        if not types_of(it) & ORG_TYPES:
            continue
        tel, mail = clean(first(it.get("telephone"))), clean(first(it.get("email")))
        if tel and "phone" not in d:
            d["phone"] = tel
        if mail and "email" not in d:
            d["email"] = mail.replace("mailto:", "")
        addr = first(it.get("address"))
        if isinstance(addr, dict) and "address" not in d:
            d["address"] = clean(addr.get("streetAddress"))
            d["postal_code"] = clean(addr.get("postalCode"))
            d["city"] = clean(addr.get("addressLocality"))
        geo = first(it.get("geo"))
        if isinstance(geo, dict) and "lat" not in d:
            try:
                d["lat"], d["lng"] = float(geo["latitude"]), float(geo["longitude"])
            except (KeyError, TypeError, ValueError):
                pass
    return {k: v for k, v in d.items() if v is not None}


def events_from_jsonld(items: list[dict], page_url: str) -> list[dict]:
    out = []
    for it in items:
        if "event" not in " ".join(types_of(it)):
            continue
        start = clean(it.get("startDate"))
        name = clean(it.get("name"))
        if not (start and name):
            continue
        loc = first(it.get("location"))
        out.append({
            "title": name, "starts_at": start, "ends_at": clean(it.get("endDate")),
            "location": clean(loc.get("name")) if isinstance(loc, dict) else clean(loc),
            "description": clean(BeautifulSoup(str(it.get("description") or ""), "html.parser").get_text(" "))[:500]
            if it.get("description") else None,
            "source_url": clean(it.get("url")) or page_url,
        })
    return out


# ------------------------------------------------------------------ plain HTML
def org_from_html(soup: BeautifulSoup, site_host: str) -> dict:
    d: dict = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("tel:") and "phone" not in d:
            num = clean(urllib.parse.unquote(href[4:]))
            if num and PHONE_RE.match(num):
                d["phone"] = num
        elif href.lower().startswith("mailto:"):
            mail = urllib.parse.unquote(href[7:]).split("?")[0].strip()
            if not EMAIL_RE.match(mail):
                continue
            # Prefer a general address on the parish's own domain (info@, expedition@, kansli@ …)
            general = re.match(r"^(info|expedition|exp|kansli|kontakt|kyrkan|forsamling|församling|post|office)\b",
                               mail, re.I)
            own = site_host.removeprefix("www.").split(".")[-2:] == mail.split("@")[-1].split(".")[-2:]
            score = 2 * bool(general) + bool(own)
            if score > d.get("_mail_score", -1):
                d["email"], d["_mail_score"] = mail, score
    for sel in ('meta[name="description"]', 'meta[property="og:description"]'):
        m = soup.select_one(sel)
        if m and clean(m.get("content")) and len(clean(m.get("content"))) > 40:
            d["description"] = clean(m["content"])
            break
    d.pop("_mail_score", None)
    return d


# ------------------------------------------------------------------ iCal
def ical_events(text: str, source_url: str) -> list[dict]:
    text = re.sub(r"\r?\n[ \t]", "", text)        # unfold continuation lines
    out, cur = [], None
    for line in text.splitlines():
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if cur.get("SUMMARY") and cur.get("DTSTART"):
                out.append({
                    "title": unescape(cur["SUMMARY"]), "starts_at": ical_date(cur["DTSTART"]),
                    "ends_at": ical_date(cur.get("DTEND")), "location": unescape(cur.get("LOCATION")),
                    "description": (unescape(cur.get("DESCRIPTION")) or "")[:500] or None,
                    "source_url": cur.get("URL") or source_url,
                })
            cur = None
        elif cur is not None and ":" in line:
            key, val = line.split(":", 1)
            cur.setdefault(key.split(";")[0].upper(), val)
    return out


def ical_date(v: str | None) -> str | None:
    if not v:
        return None
    m = re.match(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?", v.strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss, z = m.groups()
    if not hh:
        return f"{y}-{mo}-{d}"
    t = dt.datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0))
    if z:  # UTC -> Swedish local time
        t = t.replace(tzinfo=dt.timezone.utc).astimezone(_stockholm()).replace(tzinfo=None)
    return t.isoformat(timespec="minutes")


def _stockholm():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Stockholm")
    except Exception:
        return dt.timezone(dt.timedelta(hours=1))


def unescape(s: str | None) -> str | None:
    if s is None:
        return None
    return clean(s.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


# ------------------------------------------------------------------ Svenska kyrkan calendars
SVK_TITLE = ("title", "name", "heading", "rubrik", "eventtitle", "eventname", "header")
SVK_START = ("start", "startdate", "starttime", "startdatetime", "startsat", "from", "datefrom", "date", "datum",
             "eventstart", "begin", "occurson", "time")
SVK_END = ("end", "enddate", "endtime", "enddatetime", "endsat", "to", "dateto", "eventend")
SVK_PLACE = ("place", "placename", "location", "locationname", "venue", "lokal", "plats", "room", "church")
SVK_TEXT = ("preamble", "ingress", "summary", "shortdescription", "description", "text", "intro", "teaser")
SVK_URL = ("url", "link", "href", "permalink", "pageurl")


def _get(d: dict, keys) -> object:
    low = {re.sub(r"[^a-z]", "", k.lower()): v for k, v in d.items()}
    for k in keys:
        v = low.get(k)
        if v not in (None, "", [], {}):
            if isinstance(v, dict):
                v = v.get("name") or v.get("title") or v.get("Name") or v.get("Title") or v.get("value")
            return v
    return None


def _date(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (int, float)) and v > 10**9:            # epoch seconds / milliseconds
        return dt.datetime.fromtimestamp(v / 1000 if v > 10**11 else v).isoformat(timespec="minutes")
    s = str(v).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?", s)
    if not m:
        return None
    if s.endswith("Z") and m.group(2):
        t = dt.datetime.fromisoformat(s[:19]).replace(tzinfo=dt.timezone.utc).astimezone(_stockholm())
        return t.replace(tzinfo=None).isoformat(timespec="minutes")
    return m.group(1) + (f"T{m.group(2)}" if m.group(2) and m.group(2) != "00:00" else "")


def svk_events_from_json(doc) -> list[dict]:
    out, stack = [], [doc]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            title, start = _get(x, SVK_TITLE), _date(_get(x, SVK_START))
            if isinstance(title, str) and start:
                url = _get(x, SVK_URL)
                out.append({
                    "title": clean(title), "starts_at": start, "ends_at": _date(_get(x, SVK_END)),
                    "location": clean(_get(x, SVK_PLACE)) if not isinstance(_get(x, SVK_PLACE), (list, dict)) else None,
                    "description": (clean(BeautifulSoup(str(_get(x, SVK_TEXT) or ""), "html.parser").get_text(" "))
                                    or None),
                    "source_url": (urllib.parse.urljoin("https://www.svenskakyrkan.se/", url)
                                   if isinstance(url, str) else None),
                })
            else:
                stack.extend(v for v in x.values() if isinstance(v, (list, dict)))
    return out


def svk_events_from_html(html: str, page_url: str) -> list[dict]:
    """Fallback: events in the rendered page, found by their <time datetime=...> tags."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for t in soup.find_all("time", attrs={"datetime": True}):
        box = t.find_parent(["article", "li"]) or t.parent
        head = box.find(["h2", "h3", "h4"]) if box else None
        start = _date(t["datetime"])
        if not (head and start):
            continue
        a = head.find("a", href=True) or box.find("a", href=True)
        out.append({"title": clean(head.get_text(" ")), "starts_at": start, "ends_at": None,
                    "location": None, "description": None,
                    "source_url": urllib.parse.urljoin(page_url, a["href"]) if a else page_url})
    return out


def load_svk_calendars(con) -> dict:
    stats = {"svk_parishes_with_events": 0, "svk_events": 0}
    by_org: dict[str, list[dict]] = defaultdict(list)
    for page_url, body in con.execute("SELECT page_url, body FROM raw_api_responses WHERE page_url LIKE 'svk:%'"):
        try:
            by_org[page_url[4:]] += svk_events_from_json(json.loads(body))
        except ValueError:
            continue
    for org_id, url, body in con.execute("SELECT org_id, final_url, body FROM raw_sites WHERE kind='svkcal' "
                                         "AND body<>'shared'"):
        if not by_org.get(org_id):
            by_org[org_id] += svk_events_from_html(body or "", url or "")
    # Churches sharing a pastorat calendar get the events that mention them
    shared = con.execute("SELECT s.org_id, o.name, p.org_id FROM raw_sites s JOIN organizations o ON o.id = s.org_id "
                         "JOIN raw_sites p ON p.kind='svkcal' AND p.final_url = s.final_url AND p.body<>'shared' "
                         "WHERE s.kind='svkcal' AND s.body='shared'").fetchall()
    for org_id, name, primary in shared:
        words = {w for w in re.findall(r"[a-zåäö]{4,}", (name or "").lower())} - {"kyrka", "kyrkan", "församling"}
        by_org[org_id] += [e for e in by_org.get(primary, [])
                           if words & set(re.findall(r"[a-zåäö]{4,}", f"{e['title']} {e['location']}".lower()))]

    con.execute("DELETE FROM events WHERE id LIKE 'svk-%'")
    for org_id, events in by_org.items():
        seen, n = set(), 0
        for e in events:
            if not in_window(e["starts_at"]):
                continue
            key = (e["starts_at"][:16], (e["title"] or "").lower(), (e["location"] or "").lower())
            if key in seen:
                continue
            seen.add(key)
            eid = "svk-" + hashlib.sha1(f"{org_id}|{key}".encode()).hexdigest()[:12]
            con.execute(
                "INSERT OR REPLACE INTO events(id,org_id,title,starts_at,ends_at,location,city,category,"
                "description,source_url) VALUES (?,?,?,?,?,?,(SELECT city FROM organizations WHERE id=?),?,?,?)",
                (eid, org_id, e["title"], e["starts_at"], e["ends_at"], e["location"], org_id, None,
                 (e["description"] or "")[:500] or None, e["source_url"]))
            n += 1
        if n:
            stats["svk_parishes_with_events"] += 1
            stats["svk_events"] += n
    return stats


# ------------------------------------------------------------------ main
def in_window(start: str | None) -> bool:
    if not start:
        return False
    try:
        d = dt.date.fromisoformat(start[:10])
    except ValueError:
        return False
    today = dt.date.today()
    return today <= d <= today + dt.timedelta(days=EVENT_DAYS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kyrktorget.db", type=Path)
    args = ap.parse_args()
    con = sqlite3.connect(args.db)
    con.executescript((Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text(encoding="utf-8"))

    pages = defaultdict(list)
    for org_id, kind, status, final, body in con.execute(
            "SELECT org_id, kind, status, final_url, body FROM raw_sites WHERE status=200 AND body IS NOT NULL"):
        pages[org_id].append((kind, final, body))

    stats = defaultdict(int)
    for org_id, plist in pages.items():
        facts: dict = {}
        events: list[dict] = []
        home_url = next((f for k, f, _ in plist if k == "home"), None)
        for kind, url, body in sorted(plist, key=lambda p: ["home", "contact", "calendar", "ical"].index(p[0])
                                      if p[0] in ("home", "contact", "calendar", "ical") else 9):
            if kind == "ical":
                events += ical_events(body, url)
                continue
            soup = BeautifulSoup(body, "html.parser")
            items = jsonld_items(soup)
            for k, v in org_from_jsonld(items).items():
                facts.setdefault(k, v)
            # contact page beats home page for plain-HTML phone/e-mail
            plain = org_from_html(soup, urllib.parse.urlparse(url or "").netloc.lower())
            for k, v in plain.items():
                if kind == "contact" and k in ("phone", "email"):
                    facts[k] = v
                else:
                    facts.setdefault(k, v)
            events += events_from_jsonld(items, url)

        if home_url:
            facts["website"] = home_url   # resolved address (no more kyrktorget redirect links)
        if facts:
            # A site's short meta description only fills a gap; it never replaces a written presentation
            sets = ", ".join(f"{k}=COALESCE({k},?)" if k == "description" else f"{k}=?" for k in facts)
            con.execute(f"UPDATE organizations SET {sets} WHERE id=?", (*facts.values(), org_id))
            for k in facts:
                stats[k] += 1
            stats["parishes_updated"] += 1

        seen = set()
        for e in events:
            if not in_window(e["starts_at"]):
                continue
            key = (e["starts_at"][:16], (e["title"] or "").lower())
            if key in seen:
                continue
            seen.add(key)
            eid = "site-" + hashlib.sha1(f"{org_id}|{key}".encode()).hexdigest()[:12]
            con.execute(
                "INSERT OR REPLACE INTO events(id,org_id,title,starts_at,ends_at,location,city,category,"
                "description,source_url) VALUES (?,?,?,?,?,?,(SELECT city FROM organizations WHERE id=?),?,?,?)",
                (eid, org_id, e["title"], e["starts_at"], e["ends_at"], e["location"], org_id, None,
                 e["description"], e["source_url"]),
            )
            stats["events"] += 1

    for k, v in load_svk_calendars(con).items():
        stats[k] = v

    # Drop Kyrktorget calendar copies of events the parish itself publishes at the same time
    con.execute("""
        DELETE FROM events WHERE id NOT LIKE 'site-%' AND EXISTS (
            SELECT 1 FROM events s WHERE s.id LIKE 'site-%' AND s.org_id = events.org_id
            AND substr(s.starts_at,1,16) = substr(replace(events.starts_at,' ','T'),1,16))""")
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('websites_normalised_at', ?)",
                (dt.datetime.now().isoformat(timespec="seconds"),))
    con.commit()
    con.close()
    print("Updated from parish websites:", dict(stats))


if __name__ == "__main__":
    main()
