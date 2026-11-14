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
import urllib.parse
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
              "kyrkor", "pastorat", "svenska", "kyrkans", "kapell", "kapellet", "s:t", "s:ta", "sankt", "sankta",
              "katolska", "katolsk", "av"}


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
    row = con.execute("SELECT body FROM raw_api_responses WHERE url LIKE '%overpass%' AND page_url<>'osm-svk-churches'"
                      " ORDER BY id DESC LIMIT 1").fetchone()
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


NOT_SVK_OSM = re.compile(r"katolsk|catholic|ortodox|orthodox|pingst|baptist|missions|equmenia|metodist|advent|"
                         r"frälsnings|frikyrk|filadelfia|evangeliska frikyrkan|efk|elm|lutherska bekännelse", re.I)


def load_osm_svk_churches(con) -> int:
    """Add every Svenska kyrkan church from OpenStreetMap that is not already in the database."""
    row = con.execute("SELECT body FROM raw_api_responses WHERE page_url='osm-svk-churches' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return 0
    try:
        elements = json.loads(row[0]).get("elements", [])
    except ValueError:
        return 0
    existing = con.execute("SELECT id, name, lat, lng, municipality, county, website FROM organizations "
                           "WHERE lat IS NOT NULL AND id NOT LIKE 'osm-%'").fetchall()
    grid = defaultdict(list)
    coarse = defaultdict(list)          # ~20 km cells, to find the nearest municipality/county
    for e in existing:
        grid[(round(e[2], 2), round(e[3], 2))].append(e)
        coarse[(round(e[2] * 5), round(e[3] * 5))].append(e)
    parish_site = {clean(n).lower(): w for n, w in con.execute(
        "SELECT name, website FROM organizations WHERE website IS NOT NULL AND id NOT LIKE 'osm-%'") if n}

    def nearest_area(lat, lng):
        best = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for oid, oname, olat, olng, muni, county, web in coarse.get((round(lat * 5) + dy, round(lng * 5) + dx), []):
                    d = metres(lat, lng, olat, olng)
                    if muni and (best is None or d < best[0]):
                        best = (d, muni, county)
        return best

    def nearby(lat, lng):
        for dy in (-0.01, 0, 0.01):
            for dx in (-0.01, 0, 0.01):
                yield from grid.get((round(lat + dy, 2), round(lng + dx, 2)), [])

    con.execute("DELETE FROM organizations WHERE id LIKE 'osm-%'")
    n = 0
    for e in elements:
        t = e.get("tags", {})
        name = clean(t.get("name"))
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lng = e.get("lon") or (e.get("center") or {}).get("lon")
        if not name or lat is None or lng is None:
            continue
        if NOT_SVK_OSM.search(" ".join([name, t.get("operator", ""), t.get("denomination", "")])):
            continue
        if t.get("historic") in ("ruins", "church_ruin") or t.get("ruins") == "yes" or "ruin" in name.lower() \
                or t.get("disused:amenity") or t.get("abandoned") == "yes":
            continue
        # Already in the database? (same spot, or close by with a similar name)
        dup = False
        for oid, oname, olat, olng, muni, county, web in nearby(lat, lng):
            dist = metres(lat, lng, olat, olng)
            if dist <= 60 or (dist <= 400 and similarity(name, oname) >= 0.5):
                dup = True
                break
        if dup:
            continue
        best = nearest_area(float(lat), float(lng))
        operator = clean(t.get("operator"))
        web = t.get("website") or t.get("contact:website") or t.get("url")
        if web and not web.startswith("http"):
            web = "https://" + web
        if not web and operator:
            web = parish_site.get(operator.lower())     # the parish's site, when we know it
        street = " ".join(filter(None, [t.get("addr:street"), t.get("addr:housenumber")])) or None
        city = clean(t.get("addr:city") or t.get("addr:place"))
        muni = best[1] if best and best[0] < 25_000 else None
        county = best[2] if best and best[0] < 40_000 else None
        oid = f"osm-{e.get('type', 'x')[0]}{e.get('id')}"
        con.execute(
            "INSERT OR REPLACE INTO organizations(id,name,kind,denomination,address,postal_code,city,municipality,"
            "county,website,phone,email,description,lat,lng,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, name, "Kyrka", "Svenska kyrkan", street, clean(t.get("addr:postcode")), city or muni, muni, county,
             web, clean(t.get("phone") or t.get("contact:phone")), clean(t.get("email") or t.get("contact:email")),
             f"Tillhör {operator}." if operator else None, float(lat), float(lng),
             f"https://www.openstreetmap.org/{e.get('type')}/{e.get('id')}"),
        )
        n += 1
    return n


# ------------------------------------------------------------------ denominations' own directories
DIRECTORY_SAMFUND = {"equmeniakyrkan": "Equmeniakyrkan", "pingst": "Pingströrelsen",
                     "alliansmissionen": "Alliansmissionen", "katolska": "Katolska kyrkan",
                     "efk": "Evangeliska Frikyrkan"}


def _raw_json_after(text: str, marker: str):
    i = text.find(marker)
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[i + len(marker):].lstrip())[0]
    except ValueError:
        return None


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_directories(con) -> list[dict]:
    """Church lists from the denominations' websites (fetched by fetch_directories.py)."""
    out: list[dict] = []
    for page_url, url, body in con.execute("SELECT page_url, url, body FROM raw_api_responses "
                                           "WHERE page_url LIKE 'dir:%' AND status=200"):
        src = page_url[4:]
        sf = DIRECTORY_SAMFUND.get(src)
        if not sf or not body:
            continue
        if src == "equmeniakyrkan":
            data = _raw_json_after(body, "window.FWP_JSON =") or {}
            locs = (((data.get("preload_data") or {}).get("settings") or {}).get("map") or {}).get("locations") or []
            for loc in locs:
                s = BeautifulSoup(loc.get("content") or "", "html.parser")
                labels = [x.get_text(strip=True).lower() for x in s.select(".label")]
                if labels and "equmeniakyrkan" not in labels:
                    continue       # youth/scout groups only
                h = s.find(["h3", "h2"])
                a = s.find("a", href=True)
                pos = loc.get("position") or {}
                out.append({"source": src, "samfund": sf, "name": clean(h.get_text()) if h else None,
                            "lat": _fnum(pos.get("lat")), "lng": _fnum(pos.get("lng")),
                            "street": clean((s.select_one(".street-address") or Tag(name="x")).get_text()),
                            "city": clean((s.select_one(".city") or Tag(name="x")).get_text()),
                            "website": None, "info_url": a["href"] if a else None})
        elif src == "pingst":
            try:
                markers = json.loads(body).get("markers") or []
            except ValueError:
                markers = []
            for m in markers:
                parts = [p.strip() for p in (m.get("address") or "").split(",")]
                link = (m.get("link") or "").strip()
                if link and not link.startswith("http"):
                    link = "https://" + link
                out.append({"source": src, "samfund": sf, "name": clean(m.get("title")),
                            "lat": _fnum(m.get("lat")), "lng": _fnum(m.get("lng")),
                            "street": parts[0].title() if parts and parts[0] else None,
                            "city": parts[-1].title() if len(parts) > 1 else None,
                            "website": link if link and "kyrktorget" not in link else None, "info_url": None})
        elif src == "alliansmissionen":
            for m in _raw_json_after(body, "var mapdata =") or []:
                name, _, town = (m.get("forsamling") or "").partition(",")
                web = (m.get("webaddress") or "").strip()
                if web and not web.startswith("http"):
                    web = "https://" + web
                out.append({"source": src, "samfund": sf, "name": clean(name), "lat": _fnum(m.get("lat")),
                            "lng": _fnum(m.get("lon")), "street": None, "city": clean(town),
                            "website": web or None, "info_url": None})
        elif src == "efk":
            s = BeautifulSoup(body, "html.parser")
            for a in s.select("a.lp-banner"):
                title = clean((a.select_one(".lp-banner__title") or Tag(name="x")).get_text())
                if not title:
                    continue
                name, _, town = title.rpartition(",")
                img = a.find("img", src=True)
                m = re.search(r"center=(-?[\d.]+),(-?[\d.]+)", img["src"]) if img else None
                out.append({"source": src, "samfund": sf, "name": clean(name) or title, "city": clean(town),
                            "lat": float(m.group(1)) if m else None, "lng": float(m.group(2)) if m else None,
                            "street": None, "website": None,
                            "info_url": urllib.parse.urljoin("https://efk.se/", a["href"])})
        elif src == "katolska":
            text = BeautifulSoup(body, "html.parser").get_text("\n")
            text = text.split("Församlingar i Stockholms katolska stift", 1)[-1]
            for line in text.splitlines():
                m = re.match(r"^\s*([A-ZÅÄÖ][\wåäöÅÄÖ\- ]{1,40}),\s*(.+?(församling|domkyrkoförsamling))\s*$", line)
                if m:
                    out.append({"source": src, "samfund": sf, "name": clean(m.group(2)),
                                "city": clean(m.group(1).split("-")[0]), "lat": None, "lng": None,
                                "street": None, "website": None, "info_url": None})
    return [e for e in out if e.get("name")]


def _street_key(s: str | None) -> str:
    s = (s or "").lower()
    for a, b in (("å", "a"), ("ä", "a"), ("ö", "o"), ("é", "e")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]", "", s)


def load_directories(con) -> dict:
    """Tag churches with the denomination whose directory lists them; add the ones we lack."""
    entries = parse_directories(con)
    con.execute("DELETE FROM org_samfund WHERE source='directory'")
    con.execute("DELETE FROM organizations WHERE id LIKE 'dir-%'")
    if not entries:
        return {}
    # Directory churches are never Svenska kyrkan churches: leave those out of the matching
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from samfund import classify
    orgs = [o[:8] for o in con.execute(
        "SELECT id, name, lat, lng, address, city, municipality, county, denomination, email, website "
        "FROM organizations WHERE id NOT LIKE 'osm-%'").fetchall()
        if "Svenska kyrkan" not in classify(o[8], o[1], o[9], o[10])[0]]
    all_orgs = con.execute("SELECT id, name, lat, lng, address, city, municipality, county FROM organizations").fetchall()
    def domain(u):
        h = urllib.parse.urlparse(u or "").netloc.lower()
        return h[4:] if h.startswith("www.") else h

    grid, by_town, by_domain = defaultdict(list), defaultdict(list), defaultdict(list)
    webs = {r[0]: r[1] for r in con.execute("SELECT id, website FROM organizations WHERE website IS NOT NULL")}
    for o in orgs:
        d = domain(webs.get(o[0]))
        if d and d not in ("kyrktorget.se", "facebook.com", "svenskakyrkan.se"):
            by_domain[d].append(o)
        if o[2] is not None:
            grid[(round(o[2], 2), round(o[3], 2))].append(o)
        for t in (o[5], o[6]):
            if t:
                by_town[t.lower().strip()].append(o)

    pairs = []
    for i, e in enumerate(entries):
        if e["lat"] is not None:
            for dy in (-0.01, 0, 0.01):
                for dx in (-0.01, 0, 0.01):
                    for o in grid.get((round(e["lat"] + dy, 2), round(e["lng"] + dx, 2)), []):
                        d = metres(e["lat"], e["lng"], o[2], o[3])
                        sim = similarity(e["name"], o[1])
                        same_street = bool(e["street"]) and _street_key(e["street"]) == _street_key(o[4])
                        if d <= 300 and (sim >= 0.5 or same_street) or d <= 2000 and sim >= 0.99:
                            pairs.append((sim + same_street - d / 1000, i, o[0]))
        for o in by_domain.get(domain(e["website"]), []) if e["website"] else []:
            pairs.append((3.0, i, o[0]))          # same website: certainly the same church
        if e["lat"] is None or not any(p[1] == i for p in pairs[-200:]):
            for o in by_town.get((e["city"] or "").lower().strip(), []):
                sim = similarity(e["name"], o[1])
                far = e["lat"] is not None and o[2] is not None and metres(e["lat"], e["lng"], o[2], o[3]) > 15_000
                if sim >= 0.8 or (sim >= 0.6 and not far):
                    pairs.append((sim - 0.5, i, o[0]))
    used_e, used_o, matched = set(), set(), {}
    for score, i, oid in sorted(pairs, reverse=True):
        if i in used_e or oid in used_o:
            continue
        used_e.add(i)
        used_o.add(oid)
        matched[i] = oid

    # the same church can be listed by two denominations (shared congregations): tag it twice
    stats = {"directory_matched": 0, "directory_added": 0}
    coarse = defaultdict(list)
    for o in all_orgs:
        if o[2] is not None and o[6]:
            coarse[(round(o[2] * 5), round(o[3] * 5))].append(o)
    for i, e in enumerate(entries):
        oid = matched.get(i)
        if oid:
            stats["directory_matched"] += 1
            if e["lat"] is not None:   # the denomination's own position is the more reliable one
                cur = con.execute("UPDATE organizations SET lat=?, lng=? WHERE id=? AND (lat IS NULL OR "
                                  "abs(lat-?) + abs(lng-?) > 0.05)", (e["lat"], e["lng"], oid, e["lat"], e["lng"]))
                stats["positions_corrected"] = stats.get("positions_corrected", 0) + cur.rowcount
        elif e["lat"] is not None:
            oid = f"dir-{e['source']}-{i}"
            near = None
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for o in coarse.get((round(e["lat"] * 5) + dy, round(e["lng"] * 5) + dx), []):
                        d = metres(e["lat"], e["lng"], o[2], o[3])
                        if near is None or d < near[0]:
                            near = (d, o[6], o[7])
            con.execute(
                "INSERT OR REPLACE INTO organizations(id,name,kind,denomination,address,city,municipality,county,"
                "website,lat,lng,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, e["name"], "Församling", e["samfund"], e["street"], e["city"] or (near[1] if near else None),
                 near[1] if near else None, near[2] if near else None, e["website"], e["lat"], e["lng"],
                 e["info_url"]))
            stats["directory_added"] += 1
        else:
            continue
        con.execute("INSERT OR REPLACE INTO org_samfund(org_id,samfund,source) VALUES (?,?,'directory')",
                    (oid, e["samfund"]))
        if e["website"]:
            con.execute("UPDATE organizations SET website=? WHERE id=? AND website IS NULL", (e["website"], oid))
    return stats


def load_overrides(con) -> int:
    """Hand-made corrections in data/overrides.csv (match = church id or exact name)."""
    import csv
    path = Path(__file__).resolve().parent.parent / "data" / "overrides.csv"
    if not path.exists():
        return 0
    n = 0
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(r for r in f if not r.startswith("#")):
            key = (row.get("match") or "").strip()
            if not key:
                continue
            ids = [r[0] for r in con.execute("SELECT id FROM organizations WHERE id=? OR name=?", (key, key))]
            for oid in ids:
                if (row.get("website") or "").strip():
                    con.execute("UPDATE organizations SET website=? WHERE id=?", (row["website"].strip(), oid))
                if (row.get("samfund") or "").strip():
                    con.execute("DELETE FROM org_samfund WHERE org_id=?", (oid,))
                    for sf in row["samfund"].split("|"):
                        con.execute("INSERT OR REPLACE INTO org_samfund(org_id,samfund,source) VALUES (?,?,'directory')",
                                    (oid, sf.strip()))
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
        "svenska_kyrkan_churches_added_from_openstreetmap": load_osm_svk_churches(con),
        **load_directories(con),
        "overrides": load_overrides(con),
    }
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('dataset','kyrktorget')")
    con.commit()
    total = con.execute("SELECT COUNT(*), SUM(lat IS NOT NULL) FROM organizations").fetchone()
    con.close()
    print("Normalised:", counts)
    print(f"Organisations: {total[0]} ({total[1] or 0} with coordinates)")


if __name__ == "__main__":
    main()
