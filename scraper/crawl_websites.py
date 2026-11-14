"""Fetch the latest information from each parish's OWN website.

Runs after crawl.py + normalize.py, which give us the list of parishes and their website
addresses. For every parish with a website it fetches:
  * the home page,
  * a contact page and a calendar page, if the home page links to them,
  * calendar feeds (iCal/.ics) linked from those pages.

Polite by design: obeys each site's robots.txt, waits between requests to the same site,
never sends more than one request at a time to a site, and identifies itself.
Resumable: parishes whose home page is already saved are skipped (use --refresh to redo).

Usage:
    python scraper/crawl_websites.py --db data/kyrktorget.db --max-sites 50   # test
    python scraper/crawl_websites.py --db data/kyrktorget.db                  # all
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import re
import sqlite3
import sys
import urllib.parse
import urllib.robotparser
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

USER_AGENT = (
    "Mozilla/5.0 (compatible; kyrktorget-prototype/0.3; "
    "+https://github.com/revival-henrik/Kyrktorget-prototype)"
)
SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
CONTACT_RE = re.compile(r"\b(kontakt|contact|hitta hit|om oss|personal|medarbetare)\b", re.I)
CALENDAR_RE = re.compile(r"\b(kalender|kalendarium|calendar|program|på gång|aktiviteter|händelser|gudstjänster)\b", re.I)
ICAL_RE = re.compile(r"(\.ics(\?|$)|webcal:|ical|format=ics|outputformat=ical)", re.I)
SKIP_HOSTS = ("facebook.com", "instagram.com", "youtube.com", "linkedin.com", "twitter.com", "x.com")
MAX_BYTES = 3_000_000


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


class Polite:
    """One request at a time per host, a pause between them, and robots.txt rules."""

    def __init__(self, request, delay: float):
        self.request = request
        self.delay = delay
        self.locks: dict[str, asyncio.Lock] = {}
        self.robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    async def _robots(self, url: str) -> urllib.robotparser.RobotFileParser:
        p = urllib.parse.urlparse(url)
        key = f"{p.scheme}://{p.netloc}"
        if key not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = await self.request.get(key + "/robots.txt", timeout=20_000)
                rp.parse(((await r.text()) if r.ok else "").splitlines())
            except Exception:
                rp.parse([])
            self.robots[key] = rp
        return self.robots[key]

    async def get(self, url: str):
        """Return (status, final_url, text) or raise. Status 999 = blocked by robots.txt."""
        h = host_of(url)
        lock = self.locks.setdefault(h, asyncio.Lock())
        async with lock:
            rp = await self._robots(url)
            if not rp.can_fetch(USER_AGENT, url):
                return 999, url, ""
            r = await self.request.get(url, timeout=30_000, max_redirects=10)
            body = await r.body()
            text = body[:MAX_BYTES].decode(_charset(r.headers.get("content-type", "")), errors="replace")
            await asyncio.sleep(self.delay)
            return r.status, r.url, text


def _charset(ctype: str) -> str:
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    return m.group(1) if m else "utf-8"


def same_site(a: str, b: str) -> bool:
    ha, hb = host_of(a).removeprefix("www."), host_of(b).removeprefix("www.")
    return ha == hb


def pick_links(base_url: str, html: str) -> dict[str, list[str]]:
    """Find contact/calendar pages and iCal feeds linked from a page."""
    soup = BeautifulSoup(html, "html.parser")
    out = {"contact": [], "calendar": [], "ical": []}
    base_path = urllib.parse.urlparse(base_url).path.rstrip("/")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        if href.startswith("webcal:"):
            href = "https:" + href[len("webcal:"):]
        url = urllib.parse.urljoin(base_url, href).split("#")[0]
        if not url.startswith("http"):
            continue
        label = " ".join([a.get_text(" ", strip=True), a.get("title", ""), href])
        if ICAL_RE.search(href):
            out["ical"].append(url)
        elif not same_site(url, base_url):
            continue
        # For shared platforms (e.g. svenskakyrkan.se/<parish>) stay inside the parish's own path
        elif base_path and not urllib.parse.urlparse(url).path.startswith(base_path):
            continue
        elif CALENDAR_RE.search(label):
            out["calendar"].append(url)
        elif CONTACT_RE.search(label):
            out["contact"].append(url)
    for k in out:
        out[k] = list(dict.fromkeys(out[k]))
    return out


def save(con, url, org_id, kind, status, final_url, body):
    con.execute(
        "INSERT OR REPLACE INTO raw_sites(url,org_id,kind,status,final_url,body,fetched_at) VALUES (?,?,?,?,?,?,?)",
        (url, org_id, kind, status, final_url, body, now()),
    )


async def crawl_one(polite: Polite, con, org_id: str, website: str, stats: dict) -> None:
    try:
        status, final, html = await polite.get(website)
    except Exception as exc:
        save(con, website, org_id, "error", None, None, str(exc)[:500])
        con.commit()
        stats["errors"] += 1
        return
    save(con, website, org_id, "home", status, final, html)
    con.commit()
    if status != 200 or any(h in host_of(final) for h in SKIP_HOSTS):
        stats["skipped"] += 1
        return
    stats["sites"] += 1
    links = pick_links(final, html)
    extra = [("contact", u) for u in links["contact"][:1]] + [("calendar", u) for u in links["calendar"][:1]]
    icals = links["ical"][:2]
    for kind, url in extra:
        try:
            st, fin, body = await polite.get(url)
            save(con, url, org_id, kind, st, fin, body)
            if kind == "calendar" and st == 200:
                icals += pick_links(fin, body)["ical"][:2]
        except Exception as exc:
            save(con, url, org_id, "error", None, None, str(exc)[:500])
    for url in list(dict.fromkeys(icals))[:2]:
        try:
            st, fin, body = await polite.get(url)
            if "BEGIN:VCALENDAR" in body[:2000]:
                save(con, url, org_id, "ical", st, fin, body)
                stats["ical"] += 1
        except Exception as exc:
            save(con, url, org_id, "error", None, None, str(exc)[:500])
    con.commit()


async def run(db: Path, max_sites: int, delay: float, workers: int, refresh: bool) -> None:
    con = sqlite3.connect(db)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    rows = con.execute(
        "SELECT id, website FROM organizations WHERE website LIKE 'http%' ORDER BY id"
    ).fetchall()
    done = set() if refresh else {r[0] for r in con.execute(
        "SELECT DISTINCT org_id FROM raw_sites WHERE kind IN ('home', 'error')")}
    todo = [(oid, url) for oid, url in rows if oid not in done]
    if max_sites:
        todo = todo[:max_sites]
    print(f"{len(rows)} parishes have a website; fetching {len(todo)} now ({len(done)} already done)")
    if not rows:
        print("No websites found. Run crawl.py (all pages) and normalize.py first.")
        return

    stats = {"sites": 0, "skipped": 0, "errors": 0, "ical": 0}
    async with async_playwright() as pw:
        request = await pw.request.new_context(user_agent=USER_AGENT, ignore_https_errors=False,
                                               extra_http_headers={"Accept-Language": "sv-SE,sv;q=0.9"})
        polite = Polite(request, delay)
        queue: asyncio.Queue = asyncio.Queue()
        for item in todo:
            queue.put_nowait(item)
        progress = {"n": 0}

        async def worker():
            while True:
                try:
                    oid, url = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await crawl_one(polite, con, oid, url, stats)
                progress["n"] += 1
                if progress["n"] % 10 == 0 or progress["n"] == len(todo):
                    print(f"  [{progress['n']}/{len(todo)}] ok={stats['sites']} skipped={stats['skipped']} "
                          f"errors={stats['errors']} ical={stats['ical']}")

        await asyncio.gather(*(worker() for _ in range(workers)))
        await request.dispose()
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('websites_crawled_at', ?)", (now(),))
    con.commit()
    con.close()
    print("Done:", stats)
    print("Next: python scraper/normalize_websites.py --db", db)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/kyrktorget.db", type=Path)
    ap.add_argument("--max-sites", default=0, type=int, help="0 = all")
    ap.add_argument("--delay", default=1.5, type=float, help="seconds between requests to the same site")
    ap.add_argument("--workers", default=6, type=int, help="different sites fetched in parallel")
    ap.add_argument("--refresh", action="store_true", help="fetch again even if already saved")
    a = ap.parse_args()
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9 or newer is needed")
    asyncio.run(run(a.db, a.max_sites, a.delay, a.workers, a.refresh))


if __name__ == "__main__":
    main()
