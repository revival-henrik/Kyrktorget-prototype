"""Polite crawler for kyrktorget.se.

How kyrktorget.se works (found by inspecting the site):
  * /filter lists 27 counties and ~290 municipalities. POST /api/filter?type=kommun&ids=<id>
    returns every parish in a municipality as JSON (id, title, shortname, customer).
  * POST /api/gmap/markers returns map markers (with coordinates) inside a bounding box.
  * POST /api/gmap/calmarkers returns calendar events for a date range.
  * Parish pages are plain server-rendered HTML: /<shortname> for member parishes and
    /church/<id> for the others.

The crawl runs in steps and can be stopped and restarted: pages that are already in the
database are skipped, so running it again continues where it stopped.

Usage:
    python scraper/crawl.py --db data/kyrktorget.db                 # everything
    python scraper/crawl.py --db data/kyrktorget.db --max-pages 50  # quick test
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE = "https://kyrktorget.se"
INFO_PAGES = ["appen", "sondagskola-play", "smarta-inkop", "gdpr", "om-kyrktorget",
              "sa-funkar-det", "kontakta-kyrktorget"]
# Bounding box covering Sweden, Finland and Åland
BBOX = {"_b_lat_min": 54.0, "_b_lat_max": 71.5, "_b_lng_min": 4.0, "_b_lng_max": 32.0}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36 kyrktorget-prototype-crawler/0.2"
)
SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    return con


def save_page(con, url: str, status: int, html: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = soup.get_text("\n", strip=True)
    con.execute(
        "INSERT OR REPLACE INTO raw_pages(url,status,title,text,html,fetched_at) VALUES (?,?,?,?,?,?)",
        (url, status, title, text, html, now()),
    )
    con.commit()


def save_api(con, url: str, method: str, status: int, body: str, note: str = "") -> None:
    con.execute(
        "INSERT OR IGNORE INTO raw_api_responses(page_url,url,method,status,content_type,body,fetched_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (note, url, method, status, "application/json", body, now()),
    )
    con.commit()


class Site:
    """Thin wrapper around Playwright's HTTP client (shares cookies with a real browser)."""

    def __init__(self, pw, delay: float):
        self.browser = pw.chromium.launch()
        self.context = self.browser.new_context(user_agent=USER_AGENT, locale="sv-SE")
        self.page = self.context.new_page()
        self.delay = delay
        self.token = ""

    def open(self, path: str) -> str:
        """Load a page in the browser (sets session cookies) and remember its CSRF token."""
        self.page.goto(BASE + path, wait_until="domcontentloaded", timeout=60_000)
        token = self.page.locator('meta[name="csrf-token"]').get_attribute("content")
        self.token = token or self.token
        return self.page.content()

    def get(self, path: str):
        time.sleep(self.delay)
        r = self.context.request.get(BASE + path, timeout=60_000)
        return r.status, r.text()

    def post(self, path: str, form: dict):
        time.sleep(self.delay)
        r = self.context.request.post(
            BASE + path,
            form={**form, "_token": self.token},
            headers={"X-CSRF-TOKEN": self.token, "X-Requested-With": "XMLHttpRequest",
                     "Referer": BASE + "/"},
            timeout=60_000,
        )
        return r.status, r.text()

    def resolve(self, path: str) -> str | None:
        """Where does a kyrktorget.se/redirect/<id> link lead? Reads the redirect without following it."""
        time.sleep(self.delay)
        r = self.context.request.get(BASE + path, timeout=30_000, max_redirects=0)
        loc = r.headers.get("location")
        if loc:
            return loc if loc.startswith("http") else None
        if r.status == 200:  # some sites redirect with a meta refresh or a script instead
            html = r.text()
            m = (re.search(r'http-equiv=["\']?refresh["\']?[^>]*url=([^"\'>\s]+)', html, re.I)
                 or re.search(r'(?:window\.)?location(?:\.href)?\s*=\s*["\'](https?://[^"\']+)', html))
            if m and "kyrktorget.se" not in m.group(1):
                return m.group(1)
        return None

    def close(self):
        self.browser.close()


def step_listings(site: Site, con) -> None:
    print("\n[1/5] Listing all parishes per municipality …")
    html = site.open("/filter")
    save_page(con, BASE + "/filter", 200, html)
    soup = BeautifulSoup(html, "html.parser")
    regions = {}
    for a in soup.select("[id^=btnRegion]"):
        regions[a["id"].replace("btnRegion", "")] = a.get_text(strip=True)
    kommuner = []
    for a in soup.select("[id^=btnKommun]"):
        kid = int(a["id"].replace("btnKommun", ""))
        holder = a.find_parent(class_="kommunBtn")
        cls = " ".join(holder.get("class", [])) if holder else ""
        m = re.search(r"region(\d+)", cls)
        kommuner.append((kid, a.get_text(strip=True), regions.get(m.group(1)) if m else None))
    done = {r[0] for r in con.execute("SELECT kommun_id FROM raw_listings")}
    todo = [k for k in kommuner if k[0] not in done]
    print(f"  {len(kommuner)} municipalities, {len(todo)} left to fetch")
    for i, (kid, name, lan) in enumerate(todo, 1):
        status, body = site.post(f"/api/filter?type=kommun&ids={kid},", {})
        if status != 200:
            print(f"  [warn] {name}: HTTP {status}", file=sys.stderr)
            continue
        con.execute("INSERT OR REPLACE INTO raw_listings VALUES (?,?,?,?,?)", (kid, name, lan, body, now()))
        con.commit()
        try:
            n = len(json.loads(body))
        except ValueError:
            n = "?"
        print(f"  [{i}/{len(todo)}] {name} ({lan}): {n}")


def step_markers(site: Site, con) -> None:
    print("\n[2/5] Map markers (coordinates) …")
    site.open("/")
    status, body = site.post("/api/gmap/markers", {**BBOX, "_str": "0", "_strtext": ""})
    save_api(con, BASE + "/api/gmap/markers", "POST", status, body, "all-markers")
    try:
        print(f"  {len(json.loads(body).get('result', []))} markers")
    except (ValueError, AttributeError):
        print(f"  HTTP {status}, could not parse response")


def step_calendar(site: Site, con, days: int) -> None:
    print("\n[3/5] Calendar …")
    site.open("/kalender")
    start = dt.date.today()
    stop = start + dt.timedelta(days=days)
    status, body = site.post("/api/gmap/calmarkers", {
        **BBOX, "_str": "0", "_timestart": start.isoformat(), "_timestop": stop.isoformat(), "_ort": "",
    })
    save_api(con, BASE + "/api/gmap/calmarkers", "POST", status, body, f"calendar {start}..{stop}")
    try:
        print(f"  {len(json.loads(body).get('result2', []))} events")
    except (ValueError, AttributeError):
        print(f"  HTTP {status}, could not parse response")


def parish_paths(con) -> list[str]:
    paths = []
    for (body,) in con.execute("SELECT body FROM raw_listings"):
        try:
            items = json.loads(body)
        except ValueError:
            continue
        for o in items:
            if str(o.get("customer")) == "1" and o.get("shortname"):
                paths.append((0, "/" + o["shortname"]))
            elif o.get("id"):
                paths.append((1, f"/church/{o['id']}"))
    seen, out = set(), []
    for _, p in sorted(paths):          # member parishes (richer pages) first
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def step_pages(site: Site, con, max_pages: int) -> None:
    print("\n[4/5] Parish pages and info pages …")
    have = {r[0] for r in con.execute("SELECT url FROM raw_pages")}
    paths = ["/page/" + s for s in INFO_PAGES] + parish_paths(con)
    todo = [p for p in paths if BASE + p not in have]
    if max_pages:
        todo = todo[:max_pages]
    print(f"  {len(paths)} pages known, {len(todo)} to fetch now")
    for i, path in enumerate(todo, 1):
        try:
            status, html = site.get(path)
        except Exception as exc:
            print(f"  [error] {path}: {exc}", file=sys.stderr)
            continue
        save_page(con, BASE + path, status, html)
        if i % 25 == 0 or i == len(todo):
            print(f"  [{i}/{len(todo)}] {path}")


def step_redirects(site: Site, con) -> None:
    """Turn kyrktorget.se/redirect/<id> WEBSITE links into the church's real address.
    (E-mail links are also redirects on kyrktorget.se; those are skipped.)"""
    from normalize import parse_parish   # same folder
    print("\n[5/5] Resolving website links to the churches' real addresses …")
    links = set()
    for (html,) in con.execute("SELECT html FROM raw_pages WHERE status=200 AND html LIKE '%/redirect/%'"):
        d = parse_parish(html or "")
        if d.get("website_redirect") and not d.get("website"):
            links.add(re.sub(r"^https?://(www\.)?kyrktorget\.se", "", d["website_redirect"]))
    done = {r[0] for r in con.execute("SELECT url FROM raw_sites WHERE kind='redirect'")}
    todo = sorted(p for p in links if BASE + p not in done)
    print(f"  {len(links)} links, {len(todo)} to resolve")
    for i, path in enumerate(todo, 1):
        try:
            target = site.resolve(path)
        except Exception as exc:
            print(f"  [error] {path}: {exc}", file=sys.stderr)
            continue
        con.execute("INSERT OR REPLACE INTO raw_sites(url,org_id,kind,status,final_url,body,fetched_at)"
                    " VALUES (?,?,?,?,?,?,?)", (BASE + path, "", "redirect", 200 if target else 404, target, None, now()))
        con.commit()
        if i % 25 == 0 or i == len(todo):
            print(f"  [{i}/{len(todo)}] {path} -> {target}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/kyrktorget.db", type=Path)
    ap.add_argument("--max-pages", default=0, type=int, help="max parish pages this run (0 = all)")
    ap.add_argument("--delay", default=1.0, type=float, help="seconds between requests")
    ap.add_argument("--calendar-days", default=60, type=int)
    ap.add_argument("--skip-lists", action="store_true", help="only fetch pages (lists already done)")
    args = ap.parse_args()

    con = open_db(args.db)
    with sync_playwright() as pw:
        site = Site(pw, args.delay)
        try:
            if not args.skip_lists:
                step_listings(site, con)
                step_markers(site, con)
                step_calendar(site, con, args.calendar_days)
            step_pages(site, con, args.max_pages)
            step_redirects(site, con)
        finally:
            site.close()
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('crawled_at', ?)", (now(),))
    con.commit()
    con.close()
    print("\nDone. Next: python scraper/normalize.py --db", args.db)


if __name__ == "__main__":
    main()
