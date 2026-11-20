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

- The **public site** (GitHub Pages) is built **only from OpenStreetMap** (`data/build_public_db.py`):
  real churches with names, addresses, positions and websites, openly licensed (ODbL) and credited.
  Nothing from Kyrktorget is used there. If OpenStreetMap is unreachable, fictional sample data
  (`data/seed_sample.py`) is used instead.
- **Real scraped data** stays private: the scrape workflow keeps it as short-lived
  workflow artifacts, and `.gitignore` blocks `*.db` from being committed.
- The site sets `noindex`, and every page carries an "unofficial prototype" banner.

If Kyrktorget gives permission later, you can build the public site from the real database
by pointing `build_site.py` at it.

### Local site vs. public site

Both are built by the same `site/build_site.py` and look the same; only the data differs.

| | Local site (`dist-private`, localhost) | Public site (GitHub Pages) |
|---|---|---|
| Data | `data/kyrktorget.db`: Kyrktorget, OpenStreetMap, the denominations' directories and the churches' own websites | `data/public.db`: OpenStreetMap only |
| Detail | Descriptions, activities, contact details and calendar events | Name, denomination, address, position, website, phone/e-mail where OpenStreetMap has them; few or no events |
| Built by | `./run.sh` on your Mac | GitHub Actions on every push to `main` and every Monday |
| Visible to | Only you | Anyone with the link |

OpenStreetMap data is © OpenStreetMap contributors under the Open Database License (ODbL). It may
be republished as long as it is credited (the site footer does this) and any database derived from
it is shared under the same licence.

## Project layout

```
db/schema.sql              Database schema (raw + normalised tables)
scraper/crawl.py           Playwright crawler (respects robots.txt, 2.5 s delay, skips login pages)
scraper/fetch_osm.py       Svenska kyrkan churches and church websites from OpenStreetMap
scraper/fetch_directories.py  The denominations' own church directories (Equmeniakyrkan, Pingst …)
scraper/find_websites.py   Finds and verifies websites for churches without one
scraper/crawl_websites.py  Visits each church's own website
scraper/crawl_svk_calendar.py  Svenska kyrkan calendars
scraper/normalize.py       Raw JSON/HTML -> clean tables (edit ALIASES after first run)
scraper/normalize_websites.py  Newest info from the churches' websites and calendars
scraper/samfund.py         Works out each church's denomination (samfund)
scraper/inspect_raw.py     Shows which JSON endpoints/fields were captured
data/build_public_db.py    PUBLIC database from OpenStreetMap only (used by GitHub Pages)
data/seed_sample.py        Fictional sample data (fallback if OpenStreetMap is unreachable)
data/overrides.csv         Hand-made corrections to names, websites and denominations
site/build_site.py         SQLite -> static HTML in dist/
site/templates/            Jinja2 templates
site/static/               CSS + small JS for search and calendar
.github/workflows/         pages.yml (public site), scrape.yml (private, manual)
```

## Quick start (Mac)

Double-click **Starta webbplatsen.command** to build the site from your data and open it,
**Snabbuppdatering.command** for a quick update (~3 minutes), or **Uppdatera data.command** to
fetch all new data first (can take hours). The same from Terminal: `./run.sh`, `./run.sh quick` or
`./run.sh update`.

The quick update refreshes a few churches each time, always the ones updated longest ago: 3
Svenska kyrkan calendars, 40 church websites, and up to 15 churches still missing a website.
Run it daily and every church gets its turn. Change the numbers with `QUICK_SVK`,
`QUICK_SITES` and `QUICK_FIND` in `.env`. OpenStreetMap, the denominations' directories and
Kyrktorget are only read by `update`/`full`; a weekly run is plenty. Settings such as a Brave Search key go in a file called `.env`
(see the top of `run.sh`); it is never uploaded to GitHub.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

# Public site (OpenStreetMap only) - the same as GitHub Pages
python data/build_public_db.py --out data/public.db
python site/build_site.py --db data/public.db --out dist
python -m http.server -d dist 8000          # open http://localhost:8000

# Fictional sample data (no network needed)
python data/seed_sample.py
python site/build_site.py --db data/sample.db --out dist

# Real data (private use only)
python scraper/crawl.py --db data/kyrktorget.db                  # 1. parish list + kyrktorget pages (1-2 h, resumable)
python scraper/fetch_osm.py --db data/kyrktorget.db              # 2a. churches and websites from OpenStreetMap (~2 min)
python scraper/fetch_directories.py --db data/kyrktorget.db      #     the denominations' own church directories
python scraper/normalize.py --db data/kyrktorget.db              # 2b. -> tables
python scraper/find_websites.py --db data/kyrktorget.db          # 2c. find + verify missing websites
python scraper/normalize.py --db data/kyrktorget.db              # 2d. apply them
python scraper/crawl_websites.py --db data/kyrktorget.db         # 3. each parish's own website (resumable)
python scraper/crawl_svk_calendar.py --db data/kyrktorget.db --refresh  # 3b. Svenska kyrkan calendars
python scraper/normalize_websites.py --db data/kyrktorget.db     # 4. newest info from the websites
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

### Websites for churches Kyrktorget has none for

Only member churches have a website on kyrktorget.se. `fetch_osm.py` downloads churches with a
website from OpenStreetMap, and `normalize.py` matches them one-to-one by position (within 40 m) or
by position and name (within 300 m–1 km). OpenStreetMap data is © OpenStreetMap contributors (ODbL),
credited in the site footer.

### Finding websites that nobody lists

`find_websites.py` looks for the remaining churches' websites: first the church's own e-mail domain,
then Svenska kyrkan's parish pages (svenskakyrkan.se/<parish>), and optionally a web search
(Brave Search API, `export BRAVE_API_KEY=...`) for name + town. A page is only accepted when it
matches the church: its name words, and its town, street, postal code or phone number.

### All Svenska kyrkan churches

`fetch_osm.py` also downloads every Svenska kyrkan church in Sweden from OpenStreetMap.
`normalize.py` adds the ones that are not already in the database (not within 60 m, or within 400 m
with a similar name), skips ruins and other denominations, and gives each new church the nearest
municipality/county and its parish's website when known.

### Denominations (samfund)

Every church is tagged with one or more denominations (`org_samfund`). `fetch_directories.py`
downloads the directories of Equmeniakyrkan, Pingst, Alliansmissionen, EFK and Katolska kyrkan;
`normalize.py` matches them by website, position + name, or town + name (never to a Svenska kyrkan
church), corrects wrong positions and adds churches we lack. Everything else is decided by
`scraper/samfund.py` from Kyrktorget's denomination, e-mail/website domains, the name and the
church's own website. Independent churches are "Andra frikyrkor".
Fix individual churches by hand in `data/overrides.csv`.

### Svenska kyrkan calendars

Svenska kyrkan has no public calendar feed; events are loaded into each parish's calendar page
(`svenskakyrkan.se/kalender?type=owner&id=…`) by JavaScript. `crawl_svk_calendar.py` opens that page
in a headless browser, presses "Visa fler", and saves the data the page loads.
`normalize_websites.py` turns it into events for the next 60 days.

### Parishes' own websites

`crawl_websites.py` visits the website listed for each parish (home page, plus a contact page,
a calendar page and iCal feeds when linked). It obeys each site's robots.txt, sends one request
at a time per site with a pause in between, and skips social media pages.
`normalize_websites.py` then reads schema.org data, iCal feeds, tel:/mailto: links and page
descriptions. Website data is newer, so it replaces Kyrktorget's phone, e-mail, address and
events where found. Staff names and photos are not collected.

The crawl can be stopped (Ctrl+C) and started again: pages already saved are skipped.

## GitHub Pages

Settings → Pages → Source: **GitHub Actions**. Each push to `main` deploys the public site
(OpenStreetMap data only), and it is rebuilt every Monday with fresh data.
Note: Pages on a **private** repo needs a paid GitHub plan. On GitHub Free, either make the
repo public (and don't run the scrape workflow there), or keep it private and preview locally.
