"""Read Svenska kyrkan parish calendars (svenskakyrkan.se/kalender?type=owner&id=...).

Svenska kyrkan has no public calendar feed. The calendar page shows the first 10 events and loads
more when a visitor presses "Visa fler händelser". So this opens each parish's calendar page in a
real (headless) browser, declines optional cookies, presses that button until the list covers the
next 60 days, and saves the finished page (raw_sites, kind = 'svkcal').
normalize_websites.py reads the events from it (title, time, place, theme, description).

Only churches whose website is on svenskakyrkan.se are visited. Polite: one page at a time with a
pause in between, and resumable (parishes already read are skipped; --refresh reads them again).

Usage:
    python scraper/crawl_svk_calendar.py --db data/kyrktorget.db --max 3     # first test
    python scraper/crawl_svk_calendar.py --db data/kyrktorget.db             # all
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmllib
import re
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
BASE = "https://www.svenskakyrkan.se"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36 kyrktorget-prototype/0.4"
)
CAL_LINK = re.compile(r'href="([^"]*/kalender\?type=owner[^"]*)"', re.I)
MORE_BUTTON = "button:has-text('Visa fler händelser'), button.js-show-all"
COOKIE_DECLINE = "button:has-text('Avvisa'), #CybotCookiebotDialogBodyButtonDecline"
LAST_DATE_JS = """() => { const t = [...document.querySelectorAll('svk-list-item time[datetime]')];
  return { n: document.querySelectorAll('svk-list-item').length,
           last: t.length ? t[t.length - 1].getAttribute('datetime').slice(0, 10) : null }; }"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parish_root(url: str) -> str | None:
    """https://www.svenskakyrkan.se/vallentuna/kontakt -> https://www.svenskakyrkan.se/vallentuna"""
    p = urllib.parse.urlparse(url)
    if not p.netloc.endswith("svenskakyrkan.se"):
        return None
    first = [x for x in p.path.split("/") if x][:1]
    return f"{BASE}/{first[0]}" if first else None


def calendar_url(home_html: str, root: str) -> str:
    m = CAL_LINK.search(home_html or "")
    if m:
        return urllib.parse.urljoin(BASE + "/", htmllib.unescape(m.group(1)))
    return root + "/kalender"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/kyrktorget.db", type=Path)
    ap.add_argument("--max", default=0, type=int, help="max parishes this run (0 = all)")
    ap.add_argument("--delay", default=2.0, type=float, help="seconds between parishes")
    ap.add_argument("--more", default=15, type=int, help="max presses of 'Visa fler händelser' per parish")
    ap.add_argument("--days", default=60, type=int, help="read events this many days ahead")
    ap.add_argument("--refresh", action="store_true", help="read parishes again even if done before")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    rows = con.execute("SELECT id, name, website FROM organizations WHERE website LIKE '%svenskakyrkan.se/%'"
                       " ORDER BY id").fetchall()
    done = set() if a.refresh else {r[0] for r in con.execute("SELECT org_id FROM raw_sites WHERE kind='svkcal'")}
    todo = [r for r in rows if r[0] not in done]
    # One calendar per parish site: several churches can share a pastorat's site
    seen_roots: dict[str, str] = {}
    if a.max:
        todo = todo[:a.max]
    print(f"{len(rows)} churches have a Svenska kyrkan website; reading {len(todo)} calendars now")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(user_agent=USER_AGENT, locale="sv-SE")
        page = context.new_page()
        until = (dt.date.today() + dt.timedelta(days=a.days)).isoformat()
        cookies_done = False

        for i, (oid, name, website) in enumerate(todo, 1):
            root = parish_root(website)
            if not root:
                continue
            if root in seen_roots and not a.refresh:
                # same parish site as a church we already read: reuse its calendar
                con.execute("INSERT OR REPLACE INTO raw_sites(url,org_id,kind,status,final_url,body,fetched_at)"
                            " VALUES (?,?,?,?,?,?,?)", (f"svkcal-shared:{oid}", oid, "svkcal", 200,
                                                        seen_roots[root], "shared", now()))
                con.commit()
                continue
            home = con.execute("SELECT body FROM raw_sites WHERE org_id=? AND kind IN ('found','home') AND status=200 "
                               "AND final_url LIKE '%svenskakyrkan.se%' LIMIT 1", (oid,)).fetchone()
            home_html = home[0] if home else ""
            try:
                if not CAL_LINK.search(home_html or ""):
                    page.goto(root, wait_until="domcontentloaded", timeout=60_000)
                    home_html = page.content()
                    time.sleep(a.delay / 2)
                cal = calendar_url(home_html, root)
                page.goto(cal, wait_until="networkidle", timeout=60_000)
                if not cookies_done:   # the cookie banner blocks the "show more" button
                    btn = page.locator(COOKIE_DECLINE)
                    if btn.count() and btn.first.is_visible():
                        btn.first.click(timeout=5_000)
                    cookies_done = True
                state = page.evaluate(LAST_DATE_JS)
                for _ in range(a.more):
                    if not state["last"] or state["last"] > until:
                        break
                    more = page.locator(MORE_BUTTON)
                    if not (more.count() and more.first.is_visible()):
                        break
                    more.first.click(timeout=5_000)
                    try:   # wait until new events have been added
                        page.wait_for_function(f"document.querySelectorAll('svk-list-item').length > {state['n']}",
                                               timeout=15_000)
                    except Exception:
                        break
                    state = page.evaluate(LAST_DATE_JS)
                    time.sleep(0.5)
                html = page.content()
            except Exception as exc:
                print(f"  [error] {name}: {exc}", file=sys.stderr)
                time.sleep(a.delay)
                continue
            con.execute("INSERT OR REPLACE INTO raw_sites(url,org_id,kind,status,final_url,body,fetched_at)"
                        " VALUES (?,?,?,?,?,?,?)", (f"svkcal:{oid}", oid, "svkcal", 200, cal, html, now()))
            con.commit()
            seen_roots[root] = cal
            print(f"  [{i}/{len(todo)}] {name}: {state['n']} events (until {state['last']})")
            time.sleep(a.delay)
        browser.close()
    con.close()
    print("Done. Next: python scraper/normalize_websites.py --db", a.db)


if __name__ == "__main__":
    main()
