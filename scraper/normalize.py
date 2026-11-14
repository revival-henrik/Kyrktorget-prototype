"""Turn the raw crawl into clean tables (organizations, activities, events, info_pages).

Sources (see crawl.py):
  raw_listings          -> every parish with municipality and county
  raw_api_responses     -> map markers (coordinates) and calendar events
  raw_pages             -> parish pages (contact details, activities, description) and info pages

Usage:
    python scraper/normalize.py --db data/kyrktorget.db
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

BASE = "https://kyrktorget.se"
MAPS_RE = re.compile(r"maps/place/(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)")
SKIP_LINE = re.compile(r"^(foto|photo|bild)\s*:", re.I)


def clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"[ \t ]+", " ", str(s)).strip()
    return s or None


def org_id_from_listing(o: dict) -> str | None:
    if str(o.get("customer")) == "1" and o.get("shortname"):
        return str(o["shortname"])
    return f"church-{o['id']}" if o.get("id") is not None else None


def org_id_from_url(url: str) -> str | None:
    path = url.replace(BASE, "").strip("/")
    if not path or path.startswith("page/") or "/" in path.replace("church/", "", 1):
        return None
    if path.startswith("church/"):
        return "church-" + path.split("/", 1)[1]
    return path


# ---------------------------------------------------------------- parish pages
def lines_of(node: Tag) -> list[str]:
    """Visible text of a node split on <br>/block boundaries."""
    out, buf = [], []
    for el in node.descendants:
        if isinstance(el, NavigableString):
            if el.parent and el.parent.name in ("script", "style"):
                continue
            buf.append(str(el))
        elif el.name in ("br", "p", "div", "h4", "h5", "li"):
            line = clean("".join(buf))
            if line:
                out.append(line)
            buf = []
    line = clean("".join(buf))
    if line:
        out.append(line)
    return out


def sections(panel: Tag) -> dict[str, list[str]]:
    """Split a contact panel into {HEADING: [lines]} using its <h5> headings."""
    result: dict[str, list[str]] = {}
    current = None
    buf: list[str] = []

    def flush():
        if current is not None:
            text = "".join(buf)
            result[current] = [l for l in (clean(x) for x in text.split("\n")) if l]

    for el in panel.children:
        if isinstance(el, Tag) and el.name == "h5":
            flush()
            current, buf = el.get_text(strip=True).upper(), []
        elif current is not None:
            if isinstance(el, NavigableString):
                buf.append(str(el))
            elif el.name == "br":
                buf.append("\n")
            else:
                if el.name == "a" and el.get("href", "").startswith("mailto:"):
                    buf.append(el["href"][7:])
                else:
                    buf.append(el.get_text(" "))
    flush()
    return result


def parse_parish(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    d: dict = {"activities": []}

    h3 = soup.select_one(".panel-body h3")
    if h3:
        d["name"] = clean(h3.get_text(" ", strip=True))
        head = h3.find_parent(class_="col-md-12") or h3.parent
        den = head.find("div", style=re.compile(r"float:\s*left"))
        if den:
            d["denomination"] = clean(den.get_text(" ", strip=True))
        first = h3.find_parent(class_="panel-body")
        if first:
            parts = []
            for col in first.select(".col-md-6"):
                text = "\n".join(l for l in lines_of(col) if not SKIP_LINE.match(l))
                if text:
                    parts.append(text)
            if parts:
                d["description"] = "\n\n".join(parts)[:2000]
    elif soup.title:
        d["name"] = clean(soup.title.get_text().split(" - Kyrktorget")[0])

    for panel in soup.select(".panel-body"):
        heading = panel.find(["h4", "h5"])
        label = heading.get_text(strip=True).upper() if heading else ""
        if label == "VI ERBJUDER":
            d["activities"] = [clean(b.get_text(" ", strip=True)) for b in panel.find_all("b")
                               if clean(b.get_text()) and clean(b.get_text()).upper() != "VI ERBJUDER"]
        elif label in ("TELEFON", "EPOST", "WEBB", "POSTADRESS"):
            sec = sections(panel)
            if sec.get("TELEFON"):
                d["phone"] = "; ".join(sec["TELEFON"])
            emails = [x for x in sec.get("EPOST", []) if "@" in x]
            if emails:
                d["email"] = emails[0]
            if sec.get("WEBB"):
                w = sec["WEBB"][0]
                if re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/\S*)?$", w, re.I):
                    d["website"] = w if w.startswith("http") else "https://" + w
                else:  # link text is a label; use kyrktorget's redirect link instead
                    a = panel.find("a", href=re.compile(r"/redirect/"))
                    if a:
                        d["website"] = BASE + a["href"] if a["href"].startswith("/") else a["href"]
            if sec.get("POSTADRESS"):
                d["postal_address"] = ", ".join(sec["POSTADRESS"])
        elif label == "BESÖK OSS" or panel.find("a", href=MAPS_RE):
            link = panel.find("a", href=MAPS_RE)
            if link:
                m = MAPS_RE.search(link["href"])
                d["lat"], d["lng"] = float(m.group(1)), float(m.group(2))
            block = link.parent if link else panel
            name_b = block.find("b")
            lines = [l for l in lines_of(block) if l.upper() not in ("BESÖK OSS", "HITTA HIT") and not SKIP_LINE.match(l)]
            if name_b:
                nb = clean(name_b.get_text())
                if nb in lines:
                    lines = lines[lines.index(nb) + 1:]
            if lines:
                # The street is the first line with a house number; lines before it are building names
                idx = next((i for i, l in enumerate(lines) if re.search(r"\d", l)), 0)
                street, rest = lines[idx], lines[idx + 1:]
                city = rest[-1] if rest else None
                if "," in street:  # e.g. "Roseniuskyrkan, Smala Gränd 5, Stockholm"
                    parts = [x.strip() for x in street.split(",") if x.strip()]
                    j = next((i for i, p in enumerate(parts) if re.search(r"\d", p)), 0)
                    street = parts[j]
                    if j + 1 < len(parts):
                        city = city or parts[-1]
                d["address"], d["city"] = street, city
    pc = re.search(r"\b(\d{3}\s?\d{2})\s+([A-ZÅÄÖ][\wåäöÅÄÖ\- ]+)$", d.get("postal_address") or "")
    if pc:
        d["postal_code"] = pc.group(1)
        d.setdefault("city", pc.group(2).strip())
    return d


# ---------------------------------------------------------------- steps
def load_listings(con) -> int:
    n = 0
    for kid, kommun, lan, body in con.execute("SELECT kommun_id, kommun, lan, body FROM raw_listings").fetchall():
        try:
            items = json.loads(body)
        except ValueError:
            continue
        for o in items:
            oid = org_id_from_listing(o)
            if not oid or not o.get("title"):
                continue
            url = f"{BASE}/{o['shortname']}" if not oid.startswith("church-") else f"{BASE}/church/{o['id']}"
            con.execute(
                "INSERT INTO organizations(id,name,kind,municipality,county,source_url) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET municipality=COALESCE(organizations.municipality,excluded.municipality),"
                " county=COALESCE(organizations.county,excluded.county)",
                (oid, clean(o["title"]), "Församling", kommun, lan, url),
            )
            n += 1
    return n


def load_markers(con) -> int:
    row = con.execute("SELECT body FROM raw_api_responses WHERE url LIKE '%/api/gmap/markers' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return 0
    try:
        result = json.loads(row[0]).get("result", [])
    except (ValueError, AttributeError):
        return 0
    items = result.values() if isinstance(result, dict) else result
    n = 0
    for p in items:
        try:
            lat, lng = float(p.get("lat")), float(p.get("lng"))
        except (TypeError, ValueError):
            continue
        if p.get("shortname"):
            cur = con.execute("UPDATE organizations SET lat=COALESCE(lat,?), lng=COALESCE(lng,?) WHERE id=?",
                              (lat, lng, p["shortname"]))
        elif p.get("id") is not None:
            cur = con.execute("UPDATE organizations SET lat=COALESCE(lat,?), lng=COALESCE(lng,?) WHERE id=?",
                              (lat, lng, f"church-{p['id']}"))
        else:
            cur = con.execute("UPDATE organizations SET lat=COALESCE(lat,?), lng=COALESCE(lng,?) "
                              "WHERE lat IS NULL AND name=?", (lat, lng, clean(p.get("title"))))
        n += cur.rowcount
    return n


def load_pages(con) -> int:
    n = 0
    rows = con.execute("SELECT url, html FROM raw_pages WHERE status=200").fetchall()
    for url, html in rows:
        oid = org_id_from_url(url)
        if not oid or oid in ("filter", "kalender", "jobb", "andrum", "tv", "login", "new_user"):
            continue
        d = parse_parish(html or "")
        if not d.get("name"):
            continue
        con.execute(
            "INSERT INTO organizations(id,name,kind,source_url) VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (oid, d["name"], "Församling", url),
        )
        con.execute(
            "UPDATE organizations SET name=?, denomination=COALESCE(?,denomination), address=COALESCE(?,address),"
            " postal_code=COALESCE(?,postal_code), city=COALESCE(?,city), website=COALESCE(?,website),"
            " email=COALESCE(?,email), phone=COALESCE(?,phone), description=COALESCE(?,description),"
            " lat=COALESCE(?,lat), lng=COALESCE(?,lng) WHERE id=?",
            (d["name"], d.get("denomination"), d.get("address"), d.get("postal_code"), d.get("city"),
             d.get("website"), d.get("email"), d.get("phone"), d.get("description"),
             d.get("lat"), d.get("lng"), oid),
        )
        for a in d["activities"]:
            con.execute("INSERT OR IGNORE INTO activities(org_id,category) VALUES (?,?)", (oid, a))
        n += 1
    # Fall back to the municipality as "city" when a page gave none
    con.execute("UPDATE organizations SET city=municipality WHERE city IS NULL")
    return n


def load_calendar(con) -> int:
    row = con.execute("SELECT body FROM raw_api_responses WHERE url LIKE '%/api/gmap/calmarkers' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return 0
    try:
        result = json.loads(row[0]).get("result2", [])
    except (ValueError, AttributeError):
        return 0
    items = result.values() if isinstance(result, dict) else result
    con.execute("DELETE FROM events")
    n = 0
    for i, e in enumerate(items):
        title = clean(e.get("cal_title")) or clean(e.get("name"))
        if not title:
            continue
        org = e.get("shortname") or None
        if org and not con.execute("SELECT 1 FROM organizations WHERE id=?", (org,)).fetchone():
            org = None
        city = con.execute("SELECT city FROM organizations WHERE id=?", (org,)).fetchone() if org else None
        con.execute(
            "INSERT OR REPLACE INTO events(id,org_id,title,starts_at,location,city,category,description,source_url)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (str(e.get("id") or f"cal-{i}"), org, title, clean(e.get("timestart")),
             clean(e.get("title")), city[0] if city else None, clean(e.get("category")),
             clean(e.get("name")) if e.get("name") != title else None,
             f"{BASE}/{e['shortname']}" if e.get("shortname") else None),
        )
        n += 1
    return n


def load_info_pages(con) -> int:
    n = 0
    for url, html in con.execute("SELECT url, html FROM raw_pages WHERE url LIKE '%/page/%' AND status=200").fetchall():
        slug = url.rstrip("/").rsplit("/page/", 1)[-1]
        soup = BeautifulSoup(html or "", "html.parser")
        for t in soup(["script", "style", "noscript", "nav", "form"]):
            t.decompose()
        h2 = soup.select_one(".panel-body h2, .panel-body h1") or soup.find(["h1", "h2"])
        if not h2:
            continue
        body = h2.find_parent(class_="panel-body") or h2.parent
        title = h2.get_text(" ", strip=True)
        h2.decompose()
        for t in body.find_all(True):
            t.attrs = {k: v for k, v in t.attrs.items() if k in ("href", "alt")}
            if t.name in ("img", "iframe"):
                t.decompose()
        con.execute("INSERT OR REPLACE INTO info_pages(slug,title,body_html,source_url) VALUES (?,?,?,?)",
                    (slug, title, body.decode_contents().strip(), url))
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kyrktorget.db", type=Path)
    args = ap.parse_args()
    con = sqlite3.connect(args.db)
    con.executescript((Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text(encoding="utf-8"))
    counts = {
        "listed": load_listings(con),
        "pages_parsed": load_pages(con),
        "coordinates_from_markers": load_markers(con),
        "events": load_calendar(con),
        "info_pages": load_info_pages(con),
    }
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('dataset','kyrktorget')")
    con.commit()
    total = con.execute("SELECT COUNT(*), SUM(lat IS NOT NULL) FROM organizations").fetchone()
    con.close()
    print("Normalised:", counts)
    print(f"Organisations: {total[0]} ({total[1] or 0} with coordinates)")


if __name__ == "__main__":
    main()
