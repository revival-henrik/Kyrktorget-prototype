"""Download churches with a website from OpenStreetMap (Sweden + Finland) via the Overpass API.

One request (~1-3 minutes). normalize.py then matches them to our churches by position and
name, and fills in the website for churches that Kyrktorget has no website for.
OpenStreetMap data is © OpenStreetMap contributors, licensed under ODbL.

Usage:
    python scraper/fetch_osm.py --db data/kyrktorget.db
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

from playwright.sync_api import sync_playwright

ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
QUERY = """
[out:json][timeout:300];
( area["ISO3166-1"="SE"][admin_level=2]; area["ISO3166-1"="FI"][admin_level=2]; )->.c;
(
  nwr["amenity"="place_of_worship"]["religion"="christian"]["website"](area.c);
  nwr["amenity"="place_of_worship"]["religion"="christian"]["contact:website"](area.c);
  nwr["amenity"="place_of_worship"]["religion"="christian"]["url"](area.c);
);
out center tags;
"""
SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kyrktorget.db", type=Path)
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    with sync_playwright() as pw:
        req = pw.request.new_context(user_agent="kyrktorget-prototype/0.3 (OpenStreetMap church websites)")
        for url in ENDPOINTS:
            print(f"Asking {url} … (can take a few minutes)")
            try:
                r = req.post(url, form={"data": QUERY}, timeout=420_000)
                body = r.text()
                n = len(json.loads(body).get("elements", []))
            except Exception as exc:
                print(f"  failed: {exc}")
                continue
            con.execute("DELETE FROM raw_api_responses WHERE url LIKE '%overpass%'")
            con.execute(
                "INSERT INTO raw_api_responses(page_url,url,method,status,content_type,body,fetched_at)"
                " VALUES (?,?,?,?,?,?,?)",
                ("osm-church-websites", url, "POST", r.status, "application/json", body,
                 dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
            con.commit()
            print(f"  {n} churches with a website in OpenStreetMap")
            break
        req.dispose()
    con.close()
    print("Next: python scraper/normalize.py --db", a.db)


if __name__ == "__main__":
    main()
