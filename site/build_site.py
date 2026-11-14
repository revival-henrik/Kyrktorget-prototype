"""Generate the static website from the SQLite database.

Usage:
    python site/build_site.py --db data/sample.db --out dist
    python -m http.server -d dist 8000      # preview at http://localhost:8000
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = Path(__file__).resolve().parent
SITE_NAME = "Församlingsguiden"
TAGLINE = "Konceptprototyp"

MONTHS = ["januari", "februari", "mars", "april", "maj", "juni", "juli",
          "augusti", "september", "oktober", "november", "december"]
WEEKDAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]


def parse_dt(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.fromisoformat(s) if fmt is None else dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def sv_date(s: str | None, with_time: bool = True) -> str:
    d = parse_dt(s)
    if not d:
        return s or ""
    out = f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"
    if with_time and (d.hour or d.minute):
        out += f" kl. {d:%H:%M}"
    return out


def slugify(s: str) -> str:
    s = s.lower()
    for a, b in (("å", "a"), ("ä", "a"), ("ö", "o"), ("é", "e"), ("ü", "u")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "x"


def load(db: Path) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    q = lambda sql, *a: [dict(r) for r in con.execute(sql, a)]  # noqa: E731
    orgs = q("SELECT * FROM organizations ORDER BY name COLLATE NOCASE")
    acts = defaultdict(list)
    for r in q("SELECT org_id, category FROM activities ORDER BY category"):
        acts[r["org_id"]].append(r["category"])
    for o in orgs:
        o["activities"] = acts.get(o["id"], [])
        o["slug"] = slugify(o["id"])
    by_id = {o["id"]: o for o in orgs}
    events = q("SELECT * FROM events ORDER BY starts_at")
    for e in events:
        e["org"] = by_id.get(e.get("org_id"))
        e["date_label"] = sv_date(e["starts_at"], with_time=False)
        d = parse_dt(e["starts_at"])
        e["time_label"] = f"{d:%H:%M}" if d and (d.hour or d.minute) else ""
        e["day"] = d.date().isoformat() if d else ""
    jobs = q("SELECT * FROM jobs ORDER BY COALESCE(published_at,'') DESC")
    pages = q("SELECT * FROM info_pages ORDER BY title")
    meta = {r["key"]: r["value"] for r in q("SELECT key, value FROM meta")}
    con.close()
    return {"orgs": orgs, "events": events, "jobs": jobs, "pages": pages, "meta": meta}


def build(db: Path, out: Path) -> None:
    data = load(db)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(HERE / "static", out / "static")

    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    env.filters["sv_date"] = sv_date
    env.filters["phone_number"] = lambda s: (re.search(r"\+?\d[\d \-]{5,}\d", s or "") or [""])[0]
    common = {
        "site_name": SITE_NAME,
        "tagline": TAGLINE,
        "is_sample": data["meta"].get("dataset") == "sample",
        "built_at": dt.date.today().isoformat(),
        "info_pages": data["pages"],
    }

    def render(template: str, path: str, **ctx) -> None:
        depth = path.count("/")
        root = "../" * depth
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(env.get_template(template).render(root=root, path=path, **common, **ctx), encoding="utf-8")

    orgs = data["orgs"]
    all_acts = sorted({a for o in orgs for a in o["activities"]})
    counties = sorted({o["county"] for o in orgs if o["county"]})

    # JSON for client-side search
    (out / "data").mkdir()
    (out / "data" / "organizations.json").write_text(json.dumps([
        {k: o[k] for k in ("slug", "name", "kind", "denomination", "city", "municipality", "county", "activities",
                           "lat", "lng")}
        for o in orgs
    ], ensure_ascii=False), encoding="utf-8")
    (out / "data" / "events.json").write_text(json.dumps([
        {"title": e["title"], "day": e["day"], "date_label": e["date_label"], "time": e["time_label"],
         "location": e["location"], "city": e["city"], "category": e["category"],
         "org": e["org"]["name"] if e["org"] else None, "org_slug": e["org"]["slug"] if e["org"] else None,
         "description": e["description"]}
        for e in data["events"]
    ], ensure_ascii=False), encoding="utf-8")

    upcoming = [e for e in data["events"] if e["day"] >= dt.date.today().isoformat()][:4]
    n_mapped = sum(1 for o in orgs if o["lat"] is not None and o["lng"] is not None)
    render("index.html", "index.html", orgs=orgs, activities=all_acts, counties=counties, n_mapped=n_mapped,
           upcoming=upcoming, n_orgs=len(orgs), n_events=len(data["events"]), n_jobs=len(data["jobs"]))
    for o in orgs:
        o_events = [e for e in data["events"] if e.get("org_id") == o["id"]]
        render("org.html", f"forsamling/{o['slug']}/index.html", org=o, events=o_events)
    cats = sorted({e["category"] for e in data["events"] if e["category"]})
    cities = sorted({e["city"] for e in data["events"] if e["city"]})
    render("calendar.html", "kalender/index.html", categories=cats, cities=cities)
    render("jobs.html", "jobb/index.html", jobs=data["jobs"])
    for p in data["pages"]:
        render("page.html", f"sida/{p['slug']}/index.html", page=p)
    render("about.html", "om/index.html")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Built {len(orgs)} organisations, {len(data['events'])} events, {len(data['jobs'])} jobs, "
          f"{len(data['pages'])} info pages -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/sample.db", type=Path)
    ap.add_argument("--out", default="dist", type=Path)
    a = ap.parse_args()
    build(a.db, a.out)
