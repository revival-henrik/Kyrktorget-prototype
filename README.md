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
python scraper/crawl.py --db data/kyrktorget.db --max-pages 400
python scraper/normalize.py --db data/kyrktorget.db
python scraper/inspect_raw.py data/kyrktorget.db
python site/build_site.py --db data/kyrktorget.db --out dist-private
```

## First scrape: tuning the field mapping

The site loads its data with JavaScript, so the exact JSON field names are only known after
the first crawl. Run the **Scrape (private)** workflow (Actions → Scrape → Run workflow),
download `kyrktorget-data`, and read `scrape-summary.txt`. It lists every JSON endpoint and
its field names. Add any names that are missing to `ALIASES` in `scraper/normalize.py`,
then re-run `normalize.py`. No new crawl is needed, because the raw data is kept.

If the parish list only appears after you pick a county and municipality, add those
clicks to `auto_expand()` in `crawl.py`.

## GitHub Pages

Settings → Pages → Source: **GitHub Actions**. Each push to `main` deploys the demo.
Note: Pages on a **private** repo needs a paid GitHub plan. On GitHub Free, either make the
repo public (and don't run the scrape workflow there), or keep it private and preview locally.
