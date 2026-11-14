"""Polite crawler for kyrktorget.se.

The site renders most of its content with JavaScript, so a headless browser
(Playwright/Chromium) is used. For every page we store:
  * the rendered HTML and visible text  -> raw_pages
  * every JSON response the page loads  -> raw_api_responses
The JSON responses usually contain the real data (parishes, events, jobs) and
are turned into clean tables by normalize.py.

Usage:
    python scraper/crawl.py --db data/kyrktorget.db [--max-pages 400] [--delay 2.5]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sqlite3
import sys
import urllib.parse
import urllib.robotparser
from collections import deque
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "https://kyrktorget.se"
HOST = urllib.parse.urlparse(BASE).netloc
SEEDS = [
    BASE + "/",
    BASE + "/filter",
    BASE + "/kalender",
    BASE + "/jobb",
    BASE + "/andrum",
    BASE + "/tv",
    BASE + "/page/appen",
    BASE + "/page/sondagskola-play",
    BASE + "/page/smarta-inkop",
    BASE + "/page/gdpr",
    BASE + "/page/om-kyrktorget",
    BASE + "/page/sa-funkar-det",
    BASE + "/page/kontakta-kyrktorget",
]
# Never touch account pages or anything that changes state.
SKIP_PREFIXES = ("/login", "/logout", "/new_user", "/register", "/password", "/admin", "/user")
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".mp4", ".mp3", ".zip", ".ics")
USER_AGENT = (
    "Mozilla/5.0 (compatible; kyrktorget-prototype-crawler/0.1; "
    "+https://github.com/revival-henrik/Kyrktorget-prototype)"
)
SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def normalise_url(url: str) -> str | None:
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or p.netloc not in (HOST, "www." + HOST):
        return None
    path = p.path or "/"
    if path.startswith(SKIP_PREFIXES) or path.lower().endswith(SKIP_EXT):
        return None
    return urllib.parse.urlunparse(("https", HOST, path, "", p.query, ""))


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    return con


def load_robots() -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser(BASE + "/robots.txt")
    try:
        rp.read()
    except Exception as exc:  # robots.txt missing/unreachable -> be conservative but continue
        print(f"[warn] could not read robots.txt: {exc}", file=sys.stderr)
        rp.parse([])
    return rp


async def crawl(db_path: Path, max_pages: int, delay: float) -> None:
    con = open_db(db_path)
    robots = load_robots()
    queue: deque[str] = deque(SEEDS)
    seen: set[str] = set(SEEDS)
    count = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(user_agent=USER_AGENT, locale="sv-SE")
        page = await context.new_page()

        current = {"url": None}

        async def on_response(resp):
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                return
            if urllib.parse.urlparse(resp.url).netloc not in (HOST, "www." + HOST):
                return
            try:
                body = await resp.text()
            except Exception:
                return
            con.execute(
                "INSERT OR IGNORE INTO raw_api_responses(page_url,url,method,status,content_type,body,fetched_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (current["url"], resp.url, resp.request.method, resp.status, ctype, body, now()),
            )

        page.on("response", on_response)

        while queue and count < max_pages:
            url = queue.popleft()
            if not robots.can_fetch(USER_AGENT, url):
                print(f"[robots] skip {url}")
                continue
            current["url"] = url
            try:
                resp = await page.goto(url, wait_until="networkidle", timeout=45_000)
                status = resp.status if resp else None
                await auto_expand(page)
                html = await page.content()
                title = await page.title()
                text = await page.inner_text("body")
                hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            except Exception as exc:
                print(f"[error] {url}: {exc}", file=sys.stderr)
                await asyncio.sleep(delay)
                continue

            con.execute(
                "INSERT OR REPLACE INTO raw_pages(url,status,title,text,html,fetched_at) VALUES (?,?,?,?,?,?)",
                (url, status, title, text, html, now()),
            )
            con.commit()
            count += 1
            print(f"[{count}/{max_pages}] {status} {url}")

            for href in hrefs:
                n = normalise_url(href)
                if n and n not in seen:
                    seen.add(n)
                    queue.append(n)
            await asyncio.sleep(delay)

        await browser.close()

    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('crawled_at', ?)", (now(),))
    con.commit()
    con.close()
    print(f"Done: {count} pages crawled into {db_path}")


async def auto_expand(page) -> None:
    """Click harmless 'show more' style buttons so lazy-loaded lists appear."""
    labels = ["Visa fler", "Visa mer", "Ladda fler", "Fler"]
    for _ in range(10):
        clicked = False
        for label in labels:
            btn = page.get_by_role("button", name=label)
            try:
                if await btn.count() and await btn.first.is_visible():
                    await btn.first.click(timeout=2_000)
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                    clicked = True
            except Exception:
                pass
        if not clicked:
            return


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/kyrktorget.db", type=Path)
    ap.add_argument("--max-pages", default=400, type=int)
    ap.add_argument("--delay", default=2.5, type=float, help="seconds between page loads")
    args = ap.parse_args()
    asyncio.run(crawl(args.db, args.max_pages, args.delay))


if __name__ == "__main__":
    main()
