# Församlingsguiden – konceptprototyp

An **unofficial design prototype** for a more modern parish directory, inspired by
[kyrktorget.se](https://kyrktorget.se). It is not affiliated with, sponsored by or
endorsed by Kyrktorget.

The project has two parts:

1. **Scraper → SQLite database.** A polite crawler loads kyrktorget.se in a headless
   browser and stores every page and the JSON data behind it. A normaliser then turns
   that into clean tables (organizations, activities, events, jobs, info_pages).
2. **Static site generator.** Builds a fast, mobile-friendly website from the
   database: instant search, filters by activity and county, a calendar, job listings and
   parish pages. No server, no cookies, no tracking.

## Data policy (important)

Kyrktorget's directory is a compiled database, and under Swedish/EU law it is protected as
a whole (the catalogue right, upphovsrättslagen §49). We don't have permission to
republish it, so:

- The **public demo** (GitHub Pages) is built **only from fictional sample data**
  (`data/seed_sample.py`).
- **Real scraped data** stays private: the scrape workflow keeps it as short-lived
  workflow artifacts, and `.gitignore` blocks `*.db` from being committed.
- The site sets `noindex`, and every page carries an "unofficial prototype" banner.

If Kyrktorget gives permission later, you can build the public site from the real database
by pointing `build_site.py` at it.

## Project layout

```
db/schema.sql              Database schema (raw + normalised tables)
scraper/crawl.py           Playwright crawler (respects robots.txt, 2.5 s delay, skips login pages)
scraper/normalize.py       Raw JSON/HTML -> clean tables (edit ALIASES after first run)
scraper/inspect_raw.py     Shows which JSON endpoints/fields were captured
data/seed_sample.py        Fictional demo data
site/build_site.py         SQLite -> static HTML in dist/
site/templates/            Jinja2 templates
site/static/               CSS + small JS for search and calendar
.github/workflows/         pages.yml (public demo), scrape.yml (private, manual)
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

# Demo site with fictional data
python data/seed_sample.py
python site/build_site.py --db data/sample.db --out dist
python -m http.server -d dist 8000          # open http://localhost:8000

# Real data (private use only)
python scraper/crawl.py --db data/kyrktorget.db --max-pages 50   # quick test (~10 min)
python scraper/crawl.py --db data/kyrktorget.db --skip-lists     # all ~5 000 parish pages (1-2 h, resumable)
python scraper/normalize.py --db data/kyrktorget.db
python site/build_site.py --db data/kyrktorget.db --out dist-private
python -m http.server -d dist-private 8000                       # open http://localhost:8000
```

## Map

The "Hitta församling" page has a **Lista / Karta** toggle and a **Nära mig** button. Nära mig
uses the browser's location to sort results by distance. The map uses Leaflet 1.9.4 (from unpkg,
with SRI hashes) and OpenStreetMap tiles. Only organisations with `lat`/`lng` in the database
appear on the map. If the scraped data has no coordinates, add them in `normalize.py`, either
from the source data or by geocoding the addresses.

## How the crawler gets the data

kyrktorget.se renders its pages on the server, and loads a few lists through internal endpoints:

1. `/api/filter?type=kommun&ids=<id>` lists every parish in each of ~290 municipalities.
2. `/api/gmap/markers` gives coordinates for the map.
3. `/api/gmap/calmarkers` gives calendar events for the next 60 days.
4. Each parish page (`/<shortname>` or `/church/<id>`) is parsed for denomination, description,
   activities, phone, e-mail, website, address and coordinates.

The crawl can be stopped (Ctrl+C) and started again: pages already saved are skipped.

## GitHub Pages

Settings → Pages → Source: **GitHub Actions**. Each push to `main` deploys the demo.
Note: Pages on a **private** repo needs a paid GitHub plan. On GitHub Free, either make the
repo public (and don't run the scrape workflow there), or keep it private and preview locally.
