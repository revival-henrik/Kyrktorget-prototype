"""Turn raw crawl data into clean tables (organizations, events, jobs, info_pages).

The real field names in kyrktorget.se's JSON are only known after a first
crawl, so this uses alias lists + simple heuristics. After your first run,
look at `python scraper/inspect_raw.py data/kyrktorget.db` and add any field
names you see to the ALIASES below.

Usage:
    python scraper/normalize.py --db data/kyrktorget.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup

ALIASES: dict[str, list[str]] = {
    "id": ["id", "uuid", "slug"],
    "name": ["name", "namn", "title", "titel", "church_name", "organization_name"],
    "kind": ["type", "typ", "kind", "category_type"],
    "denomination": ["denomination", "samfund", "community", "church_family"],
    "address": ["address", "adress", "street", "gatuadress", "visiting_address", "besoksadress"],
    "postal_code": ["zip", "zipcode", "postal_code", "postnummer", "postnr"],
    "city": ["city", "ort", "postort", "town"],
    "municipality": ["municipality", "kommun", "municipality_name", "kommun_namn"],
    "county": ["county", "lan", "län", "county_name", "lan_namn"],
    "website": ["website", "webbplats", "url", "homepage", "hemsida"],
    "email": ["email", "epost", "e_post", "mail"],
    "phone": ["phone", "telefon", "tel", "phone_number"],
    "description": ["description", "beskrivning", "about", "info", "text", "body", "presentation"],
    "lat": ["lat", "latitude"],
    "lng": ["lng", "lon", "long", "longitude"],
    "starts_at": ["start", "starts_at", "start_date", "startdatum", "date", "datum", "start_time", "from"],
    "ends_at": ["end", "ends_at", "end_date", "slutdatum", "end_time", "to"],
    "location": ["location", "plats", "place", "venue", "lokal"],
    "category": ["category", "kategori", "event_type", "activity", "aktivitet"],
    "employer": ["employer", "arbetsgivare", "organization", "company", "church"],
    "published_at": ["published_at", "publicerad", "created_at", "publish_date"],
    "deadline": ["deadline", "sista_ansokningsdag", "apply_by", "last_date", "expires_at"],
    "activities": ["activities", "aktiviteter", "tags", "categories", "verksamheter", "filters"],
}


def pick(obj: dict, field: str) -> Any:
    lower = {k.lower(): v for k, v in obj.items()}
    for alias in ALIASES[field]:
        v = lower.get(alias)
        if v not in (None, "", [], {}):
            if isinstance(v, dict):  # e.g. {"name": "Stockholm"}
                v = v.get("name") or v.get("namn") or v.get("title")
            return v
    return None


def walk_lists(node: Any) -> Iterable[list[dict]]:
    """Yield every list of dicts found anywhere in a JSON document."""
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node):
            yield node
        for x in node:
            yield from walk_lists(x)
    elif isinstance(node, dict):
        for v in node.values():
            yield from walk_lists(v)


def classify(api_url: str, item: dict) -> str | None:
    keys = {k.lower() for k in item}
    u = api_url.lower()
    has_name = bool(keys & set(ALIASES["name"]))
    has_date = bool(keys & set(ALIASES["starts_at"]))
    has_place = bool(keys & set(ALIASES["city"] + ALIASES["address"] + ALIASES["municipality"]))
    if not has_name:
        return None
    if "jobb" in u or "job" in u or keys & set(ALIASES["deadline"]):
        return "job"
    if has_date and ("kalender" in u or "event" in u or "calendar" in u or keys & set(ALIASES["ends_at"])):
        return "event"
    if has_place:
        return "org"
    return None


def make_id(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def as_text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    s = str(v)
    if "<" in s and ">" in s:
        s = BeautifulSoup(s, "html.parser").get_text(" ", strip=True)
    return s.strip() or None


def activity_names(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,;]", v) if x.strip()]
    out = []
    for x in v if isinstance(v, list) else [v]:
        if isinstance(x, dict):
            x = x.get("name") or x.get("namn") or x.get("title")
        if x:
            out.append(str(x).strip())
    return out


def normalise_api(con: sqlite3.Connection) -> dict[str, int]:
    counts = {"org": 0, "event": 0, "job": 0}
    rows = con.execute("SELECT url, body FROM raw_api_responses WHERE status BETWEEN 200 AND 299").fetchall()
    for api_url, body in rows:
        try:
            doc = json.loads(body)
        except (TypeError, ValueError):
            continue
        for lst in walk_lists(doc):
            for item in lst:
                kind = classify(api_url, item)
                if kind == "org":
                    oid = str(pick(item, "id") or make_id(pick(item, "name"), pick(item, "city")))
                    con.execute(
                        "INSERT OR REPLACE INTO organizations(id,name,kind,denomination,address,postal_code,city,"
                        "municipality,county,website,email,phone,description,lat,lng,source_url)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (oid, as_text(pick(item, "name")), as_text(pick(item, "kind")),
                         as_text(pick(item, "denomination")), as_text(pick(item, "address")),
                         as_text(pick(item, "postal_code")), as_text(pick(item, "city")),
                         as_text(pick(item, "municipality")), as_text(pick(item, "county")),
                         as_text(pick(item, "website")), as_text(pick(item, "email")),
                         as_text(pick(item, "phone")), as_text(pick(item, "description")),
                         pick(item, "lat"), pick(item, "lng"), api_url),
                    )
                    for cat in activity_names(pick(item, "activities")):
                        con.execute("INSERT OR IGNORE INTO activities(org_id,category) VALUES (?,?)", (oid, cat))
                    counts["org"] += 1
                elif kind == "event":
                    eid = str(pick(item, "id") or make_id(pick(item, "name"), pick(item, "starts_at")))
                    con.execute(
                        "INSERT OR REPLACE INTO events(id,title,starts_at,ends_at,location,city,category,"
                        "description,source_url) VALUES (?,?,?,?,?,?,?,?,?)",
                        (eid, as_text(pick(item, "name")), as_text(pick(item, "starts_at")),
                         as_text(pick(item, "ends_at")), as_text(pick(item, "location")),
                         as_text(pick(item, "city")), as_text(pick(item, "category")),
                         as_text(pick(item, "description")), api_url),
                    )
                    counts["event"] += 1
                elif kind == "job":
                    jid = str(pick(item, "id") or make_id(pick(item, "name"), pick(item, "employer")))
                    con.execute(
                        "INSERT OR REPLACE INTO jobs(id,employer,title,location,published_at,deadline,"
                        "description,source_url) VALUES (?,?,?,?,?,?,?,?)",
                        (jid, as_text(pick(item, "employer")), as_text(pick(item, "name")),
                         as_text(pick(item, "location") or pick(item, "city")),
                         as_text(pick(item, "published_at")), as_text(pick(item, "deadline")),
                         as_text(pick(item, "description")), api_url),
                    )
                    counts["job"] += 1
    return counts


def normalise_info_pages(con: sqlite3.Connection) -> int:
    n = 0
    for url, title, html in con.execute("SELECT url, title, html FROM raw_pages WHERE url LIKE '%/page/%'").fetchall():
        slug = url.rstrip("/").rsplit("/page/", 1)[-1]
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup.select("nav, header, footer, script, style, noscript, form, .navbar, .modal"):
            tag.decompose()
        main = soup.select_one("main, article, .content, #content, .container") or soup.body
        if not main:
            continue
        h1 = main.find(["h1", "h2"])
        page_title = h1.get_text(strip=True) if h1 else (title or slug)
        if h1:
            h1.decompose()
        for t in main.find_all(True):  # keep structure, drop inline styling/classes
            t.attrs = {k: v for k, v in t.attrs.items() if k in ("href", "src", "alt")}
        con.execute(
            "INSERT OR REPLACE INTO info_pages(slug,title,body_html,source_url) VALUES (?,?,?,?)",
            (slug, page_title, str(main), url),
        )
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kyrktorget.db", type=Path)
    args = ap.parse_args()
    con = sqlite3.connect(args.db)
    con.executescript((Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text(encoding="utf-8"))
    counts = normalise_api(con)
    counts["info_pages"] = normalise_info_pages(con)
    con.commit()
    con.close()
    print("Normalised:", counts)


if __name__ == "__main__":
    main()
