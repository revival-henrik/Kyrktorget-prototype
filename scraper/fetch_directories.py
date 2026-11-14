"""Download the denominations' own church directories.

Each denomination knows best which churches belong to it. normalize.py matches these lists to our
churches (by position and name, or by name and town), tags them with that denomination, fills in
missing websites, and adds directory churches we do not have yet.

  Equmeniakyrkan     equmeniakyrkan.se/forsamlingar/       ~900, with positions
  Pingströrelsen     pingst.se (church map)                 ~430, with positions and websites
  Alliansmissionen   alliansmissionen.se/…/hitta-forsamling ~140, with positions and websites
  Evangeliska Frikyrkan  efk.se/…/forsamlingar-i-efk.html  name, town, county (paged)
  Katolska kyrkan    katolskakyrkan.se/forsamlingsliv       44 parishes, "Town, Name"

(EFS, Livets Ord and others have no public directory; they are identified in samfund.py.)

Usage:
    python scraper/fetch_directories.py --db data/kyrktorget.db
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36 kyrktorget-prototype/0.5")

SOURCES = {
    "equmeniakyrkan": ["https://equmeniakyrkan.se/forsamlingar/"],
    "pingst": ["https://www.pingst.se/wp-json/wpgmza/v1/features/"
               "base64eJyrVkrLzClJLVKyUqqOUcpNLIjPTIlRsopRMoxR0gEJFGeUFni6FAPFomOBAsmlxSX5uW6ZqTkpELFapVoABU0Wug"],
    "alliansmissionen": ["https://alliansmissionen.se/home-page/hitta-forsamling/"],
    "katolska": ["https://www.katolskakyrkan.se/forsamlingsliv"],
    "efk": "https://efk.se/forsamlingar-i-sverige/forsamlingar-i-efk.html?lp-page={n}",
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/kyrktorget.db", type=Path)
    ap.add_argument("--delay", default=1.5, type=float)
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    now = lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")  # noqa: E731

    with sync_playwright() as pw:
        req = pw.request.new_context(user_agent=USER_AGENT, extra_http_headers={"Accept-Language": "sv-SE,sv;q=0.9"})

        def save(source: str, url: str, status: int, body: str) -> None:
            con.execute("INSERT INTO raw_api_responses(page_url,url,method,status,content_type,body,fetched_at)"
                        " VALUES (?,?,?,?,?,?,?)", (f"dir:{source}", url, "GET", status, "", body, now()))

        for source, urls in SOURCES.items():
            con.execute("DELETE FROM raw_api_responses WHERE page_url=?", (f"dir:{source}",))
            if source == "efk":
                n, pages = 1, 0
                while n <= 60:
                    url = urls.format(n=n)
                    try:
                        r = req.get(url, timeout=60_000)
                        body = r.text()
                    except Exception as exc:
                        print(f"  efk page {n}: {exc}")
                        break
                    if r.status != 200 or "/forsamlingar-i-efk/forsamlingar/" not in body:
                        break
                    save(source, url, r.status, body)
                    pages += 1
                    n += 1
                    time.sleep(a.delay)
                print(f"{source}: {pages} pages")
            else:
                for url in urls:
                    try:
                        r = req.get(url, timeout=90_000)
                        save(source, url, r.status, r.text())
                        print(f"{source}: HTTP {r.status}, {len(r.text()) // 1024} kB")
                    except Exception as exc:
                        print(f"{source}: failed ({exc})")
                    time.sleep(a.delay)
            con.commit()
        req.dispose()
    con.close()
    print("Done. Next: python scraper/normalize.py --db", a.db)


if __name__ == "__main__":
    main()
