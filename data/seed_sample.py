"""Create data/sample.db with FICTIONAL demo data for the public prototype.

Counties and municipalities are real Swedish places; every organisation,
person, address, phone number, event and job is invented. Event dates are
generated relative to today so the demo calendar always has upcoming items.

Usage:
    python data/seed_sample.py [--out data/sample.db]
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PLACES = [  # (city, municipality, county)
    ("Uppsala", "Uppsala", "Uppsala län"), ("Enköping", "Enköping", "Uppsala län"),
    ("Västerås", "Västerås", "Västmanlands län"), ("Örebro", "Örebro", "Örebro län"),
    ("Jönköping", "Jönköping", "Jönköpings län"), ("Huskvarna", "Jönköping", "Jönköpings län"),
    ("Växjö", "Växjö", "Kronobergs län"), ("Kalmar", "Kalmar", "Kalmar län"),
    ("Linköping", "Linköping", "Östergötlands län"), ("Norrköping", "Norrköping", "Östergötlands län"),
    ("Göteborg", "Göteborg", "Västra Götalands län"), ("Borås", "Borås", "Västra Götalands län"),
    ("Malmö", "Malmö", "Skåne län"), ("Lund", "Lund", "Skåne län"), ("Kristianstad", "Kristianstad", "Skåne län"),
    ("Stockholm", "Stockholm", "Stockholms län"), ("Täby", "Täby", "Stockholms län"),
    ("Umeå", "Umeå", "Västerbottens län"), ("Luleå", "Luleå", "Norrbottens län"),
    ("Gävle", "Gävle", "Gävleborgs län"), ("Sundsvall", "Sundsvall", "Västernorrlands län"),
]
PREFIXES = ["Ljusets", "Källans", "Hoppets", "Vägens", "Bergets", "Ankarets", "Brunnens", "Ljungens",
            "Klippans", "Hamnens", "Ekens", "Fyrens", "Lindens", "Stjärnans", "Älvens", "Ängens"]
SUFFIXES = ["församling", "kyrka", "kapell", "gemenskap"]
DENOMS = ["Pingstförsamling", "Equmeniakyrkan", "Svenska kyrkan", "EFK", "Frikyrka", "Katolska kyrkan",
          "Ortodox församling", "Baptistförsamling"]
ACTIVITIES = ["Gudstjänster", "Hemgrupper", "Söndagsskola", "Barnverksamhet", "Ungdomsaktiviteter",
              "Alpha / grundkurs", "Seniorverksamhet", "Kör och sång", "Bön", "Bibelstudier",
              "Samtal och själavård", "Streaming", "Teckenspråkstolkning", "Engelska gudstjänster"]
OTHER = [("Lägergården Granbacken", "Gård / camping", "Rättvik", "Rättvik", "Dalarnas län"),
         ("Stödlinjen Medvandraren", "Kristen vårdverksamhet", "Örebro", "Örebro", "Örebro län")]
STREETS = ["Kyrkogatan", "Storgatan", "Skolvägen", "Parkgatan", "Ängsvägen", "Torggatan", "Ringvägen"]

EVENT_TYPES = {
    "Gudstjänst": ["Kvällsmässa", "Lovsångskväll", "Veckomässa"],
    "Konsert": ["Julkonsert med kören", "Orgelkonsert", "Gospelkväll", "Luciakonsert"],
    "Studiecirkel": ["Alpha – introduktionskväll", "Bibelsamtal: Psaltaren", "Samtalsgrupp om tro och vardag"],
    "Bön": ["Morgonbön", "Bönekväll", "Tyst retreat"],
    "Barnaktivitet": ["Barnkör", "Sagostund och fika", "Pyssel i adventstid"],
}
SUNDAY = ["Söndagsgudstjänst", "Familjegudstjänst", "Högmässa", "Gudstjänst med nattvard"]
JOBS = [("Församlingspastor", "Heltid"), ("Barn- och ungdomsledare", "75 %"), ("Musikledare", "50 %"),
        ("Diakon", "Heltid"), ("Ekonomiansvarig", "Deltid"), ("Vaktmästare", "Heltid")]

INFO_PAGES = {
    "sa-funkar-det": ("Så funkar det", """
<p>Sök på ort eller namn och välj de verksamheter du är intresserad av. Resultatet uppdateras direkt medan du skriver.</p>
<h2>Filtrera smart</h2>
<p>Du kan välja flera verksamheter samtidigt — då visas bara de som har allt du valt. Länken i adressfältet uppdateras, så du kan dela din sökning.</p>
<h2>Kalendern</h2>
<p>I kalendern ser du vad som händer de kommande veckorna, sorterat per dag. Filtrera på ort eller typ av aktivitet.</p>"""),
    "for-forsamlingar": ("För församlingar", """
<p>I en riktig tjänst skulle församlingar själva kunna uppdatera sin information, lägga in händelser och publicera jobbannonser.</p>
<p>Den här prototypen visar bara hur det skulle kunna presenteras för besökare.</p>"""),
}


def build(out: Path) -> None:
    rnd = random.Random(42)
    if out.exists():
        out.unlink()
    con = sqlite3.connect(out)
    con.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))

    orgs = []
    used = set()
    for i, (city, muni, county) in enumerate(PLACES * 2):
        name = f"{rnd.choice(PREFIXES)} {rnd.choice(SUFFIXES)}"
        if (name, city) in used:
            continue
        used.add((name, city))
        oid = f"demo-{len(orgs) + 1:03d}"
        acts = rnd.sample(ACTIVITIES, rnd.randint(3, 8))
        if "Gudstjänster" not in acts:
            acts.insert(0, "Gudstjänster")
        slug = name.lower().split()[0].replace("å", "a").replace("ä", "a").replace("ö", "o")
        orgs.append(oid)
        con.execute("INSERT INTO organizations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            oid, f"{name} {city}", "Församling", rnd.choice(DENOMS),
            f"{rnd.choice(STREETS)} {rnd.randint(1, 60)}", f"{rnd.randint(100, 989)} {rnd.randint(10, 99)}",
            city, muni, county, f"https://example.org/{slug}-{city.lower()}",
            f"info@{slug}.example.org", f"0{rnd.randint(10, 99)}-{rnd.randint(100, 999)} {rnd.randint(10, 99)} {rnd.randint(10, 99)}",
            f"En levande gemenskap mitt i {city}. Välkommen som du är — till gudstjänst, fika och samtal. "
            f"(Fiktiv exempelförsamling.)", None, None, None))
        con.executemany("INSERT INTO activities VALUES (?,?)", [(oid, a) for a in acts])

    for j, (name, kind, city, muni, county) in enumerate(OTHER):
        oid = f"demo-x{j + 1}"
        con.execute("INSERT INTO organizations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            oid, name, kind, None, "Skogsvägen 3", "795 00", city, muni, county, None,
            "kontakt@example.org", None, "Fiktiv exempelverksamhet för prototypen.", None, None, None))
        orgs.append(oid)

    today = dt.date.today()
    rows = con.execute("SELECT id, name, city FROM organizations WHERE kind='Församling'").fetchall()
    n = 0
    for day_offset in range(0, 42):
        day = today + dt.timedelta(days=day_offset)
        k = 3 if day.weekday() == 6 else rnd.randint(0, 2)
        for _ in range(k):
            oid, oname, city = rnd.choice(rows)
            if day.weekday() == 6:
                cat, title, hour = "Gudstjänst", rnd.choice(SUNDAY), rnd.choice([10, 11])
            else:
                cat = rnd.choice(list(EVENT_TYPES))
                title = rnd.choice(EVENT_TYPES[cat])
                hour = 9 if title == "Morgonbön" else rnd.choice([10, 15]) if cat == "Barnaktivitet" else rnd.choice([18, 19])
            n += 1
            con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", (
                f"ev-{n}", oid, title, f"{day.isoformat()}T{hour:02d}:00:00", None, oname, city, cat,
                "Fri entré. Kaffe efteråt." if rnd.random() < .5 else None, None))

    for i, (title, extent) in enumerate(JOBS):
        oid, oname, city = rnd.choice(rows)
        pub = today - dt.timedelta(days=rnd.randint(1, 20))
        con.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)", (
            f"job-{i + 1}", oname, f"{title}, {extent}", city, pub.isoformat(),
            (pub + dt.timedelta(days=rnd.randint(20, 40))).isoformat(),
            "Vi söker dig som vill vara med och bygga gemenskap. (Fiktiv annons.)", None))

    for slug, (title, body) in INFO_PAGES.items():
        con.execute("INSERT INTO info_pages VALUES (?,?,?,?)", (slug, title, body.strip(), None))
    con.execute("INSERT INTO meta VALUES ('dataset','sample')")
    con.commit()
    con.close()
    print(f"Wrote {out}: {len(orgs)} organisations, {n} events, {len(JOBS)} jobs")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "sample.db"), type=Path)
    build(ap.parse_args().out)
