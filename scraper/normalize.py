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
from collections import defaultdict
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

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


CONTACT_HEADS = {"TELEFON", "EPOST", "E-POST", "WEBB", "WEBBPLATS", "HEMSIDA", "POSTADRESS", "BESÖKSADRESS"}


def sections(panel: Tag) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Split a contact block into ({HEADING: [lines]}, {HEADING: [link hrefs]}) using <h4>/<h5> headings."""
    lines: dict[str, list[str]] = {}
    links: dict[str, list[str]] = {}
    first = panel.find(["h4", "h5"])
    if not first:
        return lines, links
    current = None
    buf: list[str] = []

    def flush():
        if current is not None:
            lines[current] = [l for l in (clean(x) for x in re.sub(r"<br\s*/?>", "\n", "".join(buf), flags=re.I).split("\n")) if l]

    for el in first.parent.children:
        if isinstance(el, Comment):
            continue
        if isinstance(el, Tag) and el.name in ("h4", "h5"):
            flush()
            current, buf = el.get_text(strip=True).upper(), []
            links.setdefault(current, [])
        elif current is not None:
            if isinstance(el, NavigableString):
                buf.append(str(el))
            elif el.name == "br":
                buf.append("\n")
            elif el.name in ("ins", "script", "style"):
                continue
            else:
                for a in ([el] if el.name == "a" else el.find_all("a")):
                    if a.get("href"):
                        links[current].append(a["href"])
                if el.name == "a" and el.get("href", "").startswith("mailto:"):
                    buf.append(el["href"][7:])
                else:
                    buf.append(el.get_text(" "))
    flush()
    return lines, links


def split_address(line: str) -> tuple[str | None, str | None]:
    """'Roseniuskyrkan, Smala Gränd 5, Stockholm' -> ('Smala Gränd 5', 'Stockholm')."""
    parts = [x.strip() for x in line.split(",") if x.strip()]
    if not parts:
        return None, None
    j = next((i for i, p in enumerate(parts) if re.search(r"\d", p)), None)
    if j is None:
        return None, parts[-1] if len(parts) > 1 else None
    return parts[j], (parts[-1] if j + 1 < len(parts) else None)


def parse_parish(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    # Kyrktorget keeps the visiting address of non-member churches in an HTML comment
    hidden_visit = None
    for c in soup.find_all(string=lambda t: isinstance(t, Comment) and "Besöksadress" in t):
        m = re.search(r"Besöksadress\s*</h\d>\s*(.+)", str(c), re.S)
        if m:
            hidden_visit = clean(BeautifulSoup(m.group(1), "html.parser").get_text(" "))
    for t in soup(["script", "style", "noscript", "ins"]):
        t.decompose()
    d: dict = {"activities": []}

    # Name: <h1> on non-member pages, <h3> in the first panel on member pages
    h1 = soup.select_one(".panel-body h1")
    h3 = next((h for h in soup.select(".panel-body h3")
               if h.get_text(strip=True).upper() not in ("KONTAKTUPPGIFTER", "KONTAKT")), None)
    if h1:
        d["name"] = clean(h1.get_text(" ", strip=True))
    elif h3:
        d["name"] = clean(h3.get_text(" ", strip=True))
    elif soup.title:
        d["name"] = clean(re.split(r" - Kyrk(?:t)?orget", soup.title.get_text())[0])

    if h3 and not h1:
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

    for panel in soup.select(".panel-body"):
        heads = {h.get_text(strip=True).upper() for h in panel.find_all(["h4", "h5"])}
        if "VI ERBJUDER" in heads:
            d["activities"] = [clean(b.get_text(" ", strip=True)) for b in panel.find_all("b")
                               if clean(b.get_text()) and clean(b.get_text()).upper() != "VI ERBJUDER"]
        if heads & CONTACT_HEADS:
            sec, links = sections(panel)
            if sec.get("TELEFON"):
                d["phone"] = "; ".join(re.sub(r"^Telefon\s*:?\s*", "", x, flags=re.I) for x in sec["TELEFON"])
            emails = [re.sub(r"^E-?post\s*:?\s*", "", x, flags=re.I).strip()
                      for x in sec.get("EPOST", []) + sec.get("E-POST", []) if "@" in x]
            if emails:
                d["email"] = emails[0]
            web_head = next((h for h in ("WEBB", "WEBBPLATS", "HEMSIDA") if h in sec or h in links), None)
            if web_head:
                red = next((h for h in links.get(web_head, []) if "/redirect/" in h), None)
                if red:
                    d["website_redirect"] = BASE + red if red.startswith("/") else red
                text = (sec.get(web_head) or [""])[0]
                if re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/\S*)?$", text, re.I):
                    d["website"] = text if text.startswith("http") else "https://" + text
                else:
                    direct = next((h for h in links.get(web_head, []) if h.startswith("http")
                                   and "kyrktorget.se" not in h), None)
                    if direct:
                        d["website"] = direct
            if sec.get("POSTADRESS"):
                d["postal_address"] = ", ".join(sec["POSTADRESS"])
            if sec.get("BESÖKSADRESS") and "address" not in d:
                d["address"], d["city"] = split_address(", ".join(sec["BESÖKSADRESS"]))
        link = panel.find("a", href=MAPS_RE)
        if link:
            m = MAPS_RE.search(link["href"])
            d["lat"], d["lng"] = float(m.group(1)), float(m.group(2))
            block = link.parent
            name_b = block.find("b")
            lines = [l for l in lines_of(block)
                     if l.upper() not in ("BESÖK OSS", "HITTA HIT") and not SKIP_LINE.match(l)]
            if name_b and clean(name_b.get_text()) in lines:
                lines = lines[lines.index(clean(name_b.get_text())) + 1:]
            if lines:
                idx = next((i for i, l in enumerate(lines) if re.search(r"\d", l)), 0)
                street, rest = lines[idx], lines[idx + 1:]
                if "," in street:
                    street, city = split_address(street)
                    city = city or (rest[-1] if rest else None)
                else:
                    city = rest[-1] if rest else None
                d["address"], d["city"] = street, city

    if "address" not in d and hidden_visit:
        d["address"], d["city"] = split_address(hidden_visit)
    post = d.get("postal_address") or ""
    pc = re.search(r"\b(\d{3}\s?\d{2})\s+([A-ZÅÄÖ][\wåäöÅÄÖ\- ]+)$", post)
    if pc:
        d["postal_code"] = pc.group(1)
        d.setdefault("city", pc.group(2).strip())
        if not d.get("city"):
            d["city"] = pc.group(2).strip()
    if "address" not in d and post:
        street = post.split(",")[0].strip()
        if re.search(r"\d", street) and not re.match(r"^(box|pl|postbox)\b", street, re.I):
            d["address"] = street
    if not d.get("address"):
        d.pop("address", None)
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


def org_id_from_path(path: str | None) -> str | None:
    """'/church/2522' -> 'church-2522', '/silbodal' or 'bodakyrkan/kalender' -> first part."""
    p = (path or "").strip().strip("/")
    if not p:
        return None
    if p.startswith("church/"):
        return "church-" + p.split("/")[1]
    return p.split("/")[0]


def load_markers(con) -> int:
    """Coordinates for every church building on kyrktorget's map. The first building wins."""
    row = con.execute("SELECT body FROM raw_api_responses WHERE url LIKE '%/api/gmap/markers' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return 0
    try:
        result = json.loads(row[0]).get("result", [])
    except (ValueError, AttributeError):
        return 0
    items = result.values() if isinstance(result, dict) else result
    known = {r[0] for r in con.execute("SELECT id FROM organizations")}
    n = 0
    for p in items:
        try:
            lat, lng = float(p.get("lat")), float(p.get("lng"))
        except (TypeError, ValueError):
            continue
        candidates = [org_id_from_path(p.get("shortname"))]
        if p.get("id") is not None:
            candidates.append(f"church-{p['id']}")
        oid = next((c for c in candidates if c in known), None)
        if not oid:
            continue
        cur = con.execute("UPDATE organizations SET lat=?, lng=? WHERE id=? AND lat IS NULL", (lat, lng, oid))
        n += cur.rowcount
    return n


def load_pages(con) -> int:
    n = 0
    # Real addresses behind kyrktorget.se/redirect/<id> links (resolved by crawl.py step 5)
    resolved = {re.sub(r"^https?://(www\.)?", "", u): t for u, t in con.execute(
        "SELECT url, final_url FROM raw_sites WHERE kind='redirect' AND final_url IS NOT NULL")}
    con.execute("UPDATE organizations SET website=NULL WHERE website LIKE '%kyrktorget.se/redirect/%'")
    rows = con.execute("SELECT url, html FROM raw_pages WHERE status=200").fetchall()
    for url, html in rows:
        oid = org_id_from_url(url)
        if not oid or oid in ("filter", "kalender", "jobb", "andrum", "tv", "login", "new_user"):
            continue
        d = parse_parish(html or "")
        if not d.get("name"):
            continue
        real = resolved.get(re.sub(r"^https?://(www\.)?", "", d.get("website_redirect") or ""))
        if real:
            d["website"] = real
        elif "/redirect/" in (d.get("website") or ""):
            d["website"] = None   # unresolved: keep any address we already have rather than a redirect
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
        data = json.loads(row[0])
    except ValueError:
        return 0
    result = data.get("result2") or data.get("result") or []
    items = result.values() if isinstance(result, dict) else result
    known = {r[0]: r[1] for r in con.execute("SELECT id, city FROM organizations")}
    by_name = {r[1]: r[0] for r in con.execute("SELECT id, name FROM organizations")}
    con.execute("DELETE FROM events WHERE id NOT LIKE 'site-%'")
    n = 0
    for i, e in enumerate(items):
        title = clean(e.get("cal_title"))
        if not title:
            continue
        org = org_id_from_path(e.get("shortname"))
        if org not in known:
            org = f"church-{e['id']}" if f"church-{e.get('id')}" in known else by_name.get(clean(e.get("name")))
        start = clean(e.get("timestart"))
        if start and start.endswith(" 00:00"):
            start = start[:10]          # all-day event
        place = clean(e.get("title"))
        con.execute(
            "INSERT OR REPLACE INTO events(id,org_id,title,starts_at,location,city,category,description,source_url)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"kt-{e.get('event_id') or i}", org, title, start,
             place if place != clean(e.get("name")) else None, known.get(org), None, None,
             f"{BASE}/{e['shortname'].strip('/')}" if e.get("shortname") else None),
        )
        n += 1
    return n


STOP_WORDS = {"i", "och", "av", "på", "the", "församling", "församlingen", "församlingar", "kyrka", "kyrkan",
              "kyrkor", "pastorat", "svenska", "kyrkans", "kapell", "kapellet", "s:t", "s:ta", "sankt", "sankta"}


def name_tokens(s: str | None) -> set[str]:
    s = (s or "").lower()
    s = re.sub(r"<[^>]+>", " ", s)
    toks = set(re.findall(r"[a-zåäöéü0-9]+", s))
    return {t for t in toks if t not in STOP_WORDS and len(t) > 1}


def similarity(a: str | None, b: str | None) -> float:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    # also count prefix matches (Adventkyrkan ~ advent, Bodakyrkan ~ boda)
    hits = sum(1 for x in ta if any(x == y or (len(x) > 3 and (y.startswith(x) or x.startswith(y))) for y in tb))
    return hits / max(1, min(len(ta), len(tb)))


def metres(lat1, lng1, lat2, lng2) -> float:
    import math
    r = math.radians
    x = (r(lng2) - r(lng1)) * math.cos(r((lat1 + lat2) / 2))
    y = r(lat2) - r(lat1)
    return 6_371_000 * math.hypot(x, y)


def load_osm_websites(con) -> int:
    """Website for churches Kyrktorget has none for, matched from OpenStreetMap by position + name."""
    row = con.execute("SELECT body FROM raw_api_responses WHERE url LIKE '%overpass%' ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return 0
    try:
        elements = json.loads(row[0]).get("elements", [])
    except ValueError:
        return 0
    places = []
    for e in elements:
        t = e.get("tags", {})
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lng = e.get("lon") or (e.get("center") or {}).get("lon")
        web = t.get("website") or t.get("contact:website") or t.get("url")
        if lat is None or lng is None or not web:
            continue
        web = web.split(";")[0].strip()
        if not web.startswith("http"):
            web = "https://" + web
        places.append((float(lat), float(lng), " ".join(filter(None, [t.get("name"), t.get("operator"), t.get("alt_name")])), web))
    # coarse grid so each church is only compared with nearby places
    grid = defaultdict(list)
    for p in places:
        grid[(round(p[0], 2), round(p[1], 2))].append(p)
    pairs = []
    for oid, name, lat, lng in con.execute(
            "SELECT id, name, lat, lng FROM organizations WHERE website IS NULL AND lat IS NOT NULL").fetchall():
        for dy in (-0.01, 0, 0.01):
            for dx in (-0.01, 0, 0.01):
                for p in grid.get((round(lat + dy, 2), round(lng + dx, 2)), []):
                    plat, plng, pname, web = p
                    dist = metres(lat, lng, plat, plng)
                    sim = similarity(name, pname)
                    if dist <= 40 or (dist <= 300 and sim >= 0.5) or (dist <= 1000 and sim >= 0.99):
                        pairs.append((sim - dist / 1000, oid, id(p), web))
    # one-to-one: best pairs first; each church and each OpenStreetMap place used once
    used_org, used_place, n = set(), set(), 0
    for score, oid, pid, web in sorted(pairs, reverse=True):
        if oid in used_org or pid in used_place:
            continue
        used_org.add(oid)
        used_place.add(pid)
        con.execute("UPDATE organizations SET website=? WHERE id=?", (web, oid))
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
    con.execute("UPDATE organizations SET website=NULL")   # rebuilt below from all sources
    counts = {
        "listed": load_listings(con),
        "pages_parsed": load_pages(con),
        "coordinates_from_markers": load_markers(con),
        "events": load_calendar(con),
        "info_pages": load_info_pages(con),
        "websites_found_and_verified": con.execute(
            "UPDATE organizations SET website=(SELECT final_url FROM raw_sites r WHERE r.org_id=organizations.id"
            " AND r.kind='found' AND r.final_url IS NOT NULL LIMIT 1) WHERE website IS NULL AND id IN"
            " (SELECT org_id FROM raw_sites WHERE kind='found' AND final_url IS NOT NULL)").rowcount,
        "websites_from_openstreetmap": load_osm_websites(con),
    }
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('dataset','kyrktorget')")
    con.commit()
    total = con.execute("SELECT COUNT(*), SUM(lat IS NOT NULL) FROM organizations").fetchone()
    con.close()
    print("Normalised:", counts)
    print(f"Organisations: {total[0]} ({total[1] or 0} with coordinates)")


if __name__ == "__main__":
    main()
