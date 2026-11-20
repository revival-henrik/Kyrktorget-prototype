"""Build the PUBLIC database from OpenStreetMap: every Christian church in Sweden.

OpenStreetMap data is © OpenStreetMap contributors and licensed under the Open Database License
(ODbL), so it may be shown publicly with credit (the site footer does that). Unlike the private
version, nothing here comes from Kyrktorget.

What is included: name, denomination, street address, postal code, town, municipality, county,
position, website, phone, e-mail and the parish it belongs to (operator) — whatever OpenStreetMap has.
Ruins, disused churches and funeral chapels are left out.

Usage:
    python data/build_public_db.py --out data/public.db
    python data/build_public_db.py --out data/public.db --from-file osm.json   # offline/testing
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
from samfund import classify  # noqa: E402

ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
CHURCHES = 'nwr["amenity"="place_of_worship"]["religion"="christian"](area.a);'
# One pass per county and one per municipality, each area printed before the churches inside it,
# so every church gets its county and municipality.
QUERY = """
[out:json][timeout:900];
area["ISO3166-1"="SE"][admin_level=2]->.se;
rel(area.se)["boundary"="administrative"]["admin_level"="{level}"];
map_to_area->.areas;
foreach.areas->.a(
  .a out tags;
  {churches}
  out center tags;
);
"""
USER_AGENT = "kyrktorget-prototype/1.0 (public demo built from OpenStreetMap)"
SKIP_NAME = re.compile(r"ruin|gravkapell|begravningskapell|krematori|minneslund|klockstapel", re.I)


def overpass(level: int) -> dict:
    data = urllib.parse.urlencode({"data": QUERY.format(level=level, churches=CHURCHES)}).encode()
    last = None
    for url in ENDPOINTS:
        for attempt in range(2):
            try:
                print(f"Asking {url} (admin level {level}) …", flush=True)
                req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=1000) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as exc:   # busy servers answer 429/504: wait and retry
                last = exc
                print(f"  failed: {exc}", flush=True)
                time.sleep(30)
    raise SystemExit(f"OpenStreetMap could not be reached: {last}")


def osm_denomination(t: dict) -> str | None:
    """Turn OpenStreetMap's denomination/operator tags into words samfund.py understands."""
    d = (t.get("denomination") or "").lower()
    op = t.get("operator") or ""
    text = f"{op} {t.get('name', '')}"
    if re.search(r"equmenia", text, re.I):
        return "Equmeniakyrkan"
    if re.search(r"\bEFS\b", text):
        return "EFS"
    if re.search(r"pingst|filadelfia", text, re.I) or d == "pentecostal":
        return "Pingströrelsen"
    if re.search(r"evangeliska frikyrkan|\bEFK\b", text):
        return "Evangeliska Frikyrkan"
    if re.search(r"alliansmission", text, re.I):
        return "Svenska Alliansmissionen"
    if d in ("catholic", "roman_catholic") or re.search(r"katolsk", text, re.I):
        return "Katolska kyrkan"
    if "orthodox" in d or re.search(r"ortodox", text, re.I):
        return "Ortodox"
    if d in ("adventist", "seventh_day_adventist"):
        return "Adventistsamfundet"
    if d == "salvation_army" or re.search(r"frälsningsarm", text, re.I):
        return "Frälsningsarmén"
    if re.search(r"svenska kyrkan|församling|pastorat", op, re.I) or d in ("church_of_sweden", "lutheran") \
            and not re.search(r"missions|bönhus|ELM|bibeltrogna", text, re.I):
        return "Svenska kyrkan"
    return None


# Municipalities whose own name ends in -s (so "Borås kommun" is not a genitive), and irregular ones
ENDS_IN_S = {"Borås", "Bollnäs", "Vännäs", "Strängnäs", "Mönsterås", "Västerås", "Kramfors", "Hagfors", "Grums",
             "Robertsfors", "Mörbylånga"}
IRREGULAR = {"Falu": "Falun", "Region Gotland": "Gotland", "Gotlands": "Gotland"}


def municipality(area_name: str | None) -> str | None:
    """'Stockholms kommun' -> 'Stockholm', 'Borås kommun' -> 'Borås', 'Falu kommun' -> 'Falun'."""
    if not area_name:
        return None
    n = re.sub(r"\s+(kommun|stad)$", "", area_name.strip(), flags=re.I)
    if n in IRREGULAR:
        return IRREGULAR[n]
    if n.endswith("s") and n not in ENDS_IN_S:
        n = n[:-1]
    return n


def metres(a, b, c, d) -> float:
    r = math.radians
    return 6_371_000 * math.hypot((r(d) - r(b)) * math.cos(r((a + c) / 2)), r(c) - r(a))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "data" / "public.db"), type=Path)
    ap.add_argument("--from-file", type=Path, help="read saved Overpass answers (JSON list [counties, municipalities])")
    a = ap.parse_args()

    if a.from_file:
        counties, munis = json.loads(a.from_file.read_text(encoding="utf-8"))
    else:
        counties = overpass(4)
        time.sleep(10)
        munis = overpass(7)

    county_of, muni_of, elements = {}, {}, {}
    for result, target in ((counties, county_of), (munis, muni_of)):
        current = None
        for e in result.get("elements", []):
            if e.get("type") == "area":
                current = (e.get("tags") or {}).get("name")
                continue
            key = f"{e['type'][0]}{e['id']}"
            target[key] = current
            elements[key] = e

    if a.out.exists():
        a.out.unlink()
    con = sqlite3.connect(a.out)
    con.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    kept: list[tuple[str, float, float]] = []
    n = 0
    for key, e in elements.items():
        t = e.get("tags") or {}
        name = (t.get("name") or "").strip()
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lng = e.get("lon") or (e.get("center") or {}).get("lon")
        if not name or lat is None or SKIP_NAME.search(name):
            continue
        if t.get("historic") in ("ruins", "church_ruin") or t.get("ruins") == "yes" or t.get("abandoned") == "yes" \
                or t.get("disused") == "yes" or "disused:amenity" in t:
            continue
        # the same church mapped twice (as a point and as a building)
        if any(n2 == name and metres(lat, lng, la, lo) < 150 for n2, la, lo in kept[-400:]):
            continue
        kept.append((name, lat, lng))
        web = t.get("website") or t.get("contact:website") or t.get("url")
        if web and not web.startswith("http"):
            web = "https://" + web
        street = " ".join(filter(None, [t.get("addr:street"), t.get("addr:housenumber")])) or None
        muni = municipality(muni_of.get(key))
        city = t.get("addr:city") or t.get("addr:place") or muni
        operator = t.get("operator")
        denom = osm_denomination(t)
        oid = f"osm-{key}"
        con.execute(
            "INSERT OR REPLACE INTO organizations(id,name,kind,denomination,address,postal_code,city,municipality,"
            "county,website,phone,email,description,lat,lng,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, name, "Kyrka", denom, street, t.get("addr:postcode"), city, muni, county_of.get(key), web,
             t.get("phone") or t.get("contact:phone"), t.get("email") or t.get("contact:email"),
             f"Tillhör {operator}." if operator else None, float(lat), float(lng),
             f"https://www.openstreetmap.org/{e['type']}/{e['id']}"))
        labels, how = classify(denom, name, t.get("email") or t.get("contact:email"), web)
        for label in labels:
            con.execute("INSERT OR REPLACE INTO org_samfund(org_id,samfund,source) VALUES (?,?,?)",
                        (oid, label, "openstreetmap" if how == "kyrktorget" else how))
        n += 1
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('dataset','openstreetmap')")
    con.commit()
    con.close()
    print(f"Wrote {a.out}: {n} churches from OpenStreetMap")


if __name__ == "__main__":
    main()
