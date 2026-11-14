"""Print a summary of what the crawler captured, to help tune normalize.py.

Usage:
    python scraper/inspect_raw.py data/kyrktorget.db
"""
import json
import sqlite3
import sys
from collections import Counter

con = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "data/kyrktorget.db")
print("Pages:", con.execute("SELECT COUNT(*) FROM raw_pages").fetchone()[0])
print("JSON responses:", con.execute("SELECT COUNT(*) FROM raw_api_responses").fetchone()[0])
print("\nJSON endpoints (path -> count, sample keys):")
seen: dict[str, Counter] = {}
for url, body in con.execute("SELECT url, body FROM raw_api_responses"):
    path = url.split("?")[0]
    try:
        doc = json.loads(body)
    except Exception:
        continue
    item = doc[0] if isinstance(doc, list) and doc else doc
    if isinstance(item, dict) and "data" in item and isinstance(item["data"], list) and item["data"]:
        item = item["data"][0]
    keys = sorted(item.keys())[:25] if isinstance(item, dict) else [type(item).__name__]
    seen.setdefault(path, Counter())[tuple(keys)] += 1
for path, c in sorted(seen.items()):
    keys, n = c.most_common(1)[0]
    print(f"  {path}  ({sum(c.values())}x)\n      keys: {', '.join(keys)}")
for t in ("organizations", "activities", "events", "jobs", "info_pages"):
    print(f"{t}: {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
