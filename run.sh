#!/bin/bash
# One command for everything. Usage:
#
#   ./run.sh           rebuild the site from the data you have and open it   (~1 minute)
#   ./run.sh update    fetch new data first, then rebuild and open           (minutes to hours)
#   ./run.sh full      like update, but also re-lists all churches on Kyrktorget
#
# Optional settings go in a file called .env next to this script (never uploaded to GitHub):
#   BRAVE_API_KEY=your-key        enables web search for missing websites
#   SEARCH_MAX=900                max churches to search per run (900 ≈ Brave's free monthly credit)
#   PORT=8000                     port for the local website

set -e
cd "$(dirname "$0")"
MODE="${1:-build}"
DB=data/kyrktorget.db
step() { printf "\n\033[1;32m==> %s\033[0m\n" "$1"; }

# --- settings -------------------------------------------------------------
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PORT="${PORT:-8000}"
SEARCH_MAX="${SEARCH_MAX:-900}"

# --- Python environment (created automatically the first time) ------------
if [ ! -d .venv ]; then
  step "Setting up Python (first run only)"
  python3 -m venv .venv
fi
. .venv/bin/activate
if [ ! -f .venv/.deps-ok ] || [ requirements.txt -nt .venv/.deps-ok ]; then
  step "Installing packages"
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
  python -m playwright install chromium
  touch .venv/.deps-ok
fi

# --- latest code ----------------------------------------------------------
if git rev-parse --git-dir >/dev/null 2>&1; then
  step "Getting the latest version from GitHub"
  git pull --ff-only || echo "(could not update from GitHub - continuing with the current version)"
fi

# Keep the Mac awake during long runs
KEEP_AWAKE=""
command -v caffeinate >/dev/null && KEEP_AWAKE="caffeinate -i"

# --- fetch data -------------------------------------------------------------
if [ "$MODE" = "update" ] || [ "$MODE" = "full" ]; then
  if [ "$MODE" = "full" ] || [ ! -f "$DB" ]; then
    step "1/5 Kyrktorget: listing all churches and reading their pages"
    $KEEP_AWAKE python scraper/crawl.py --db "$DB"
  else
    step "1/5 Kyrktorget: reading any church pages not fetched yet"
    $KEEP_AWAKE python scraper/crawl.py --db "$DB" --skip-lists
  fi
  step "2/5 OpenStreetMap: church websites"
  python scraper/fetch_osm.py --db "$DB" || echo "(OpenStreetMap not reachable - skipped)"
  python scraper/normalize.py --db "$DB"
  step "3/5 Finding missing websites"
  python scraper/find_websites.py --db "$DB" --recheck
  $KEEP_AWAKE python scraper/find_websites.py --db "$DB" --max "$SEARCH_MAX"
  python scraper/normalize.py --db "$DB"
  step "4/5 Reading the churches' own websites"
  $KEEP_AWAKE python scraper/crawl_websites.py --db "$DB"
fi

# --- build ------------------------------------------------------------------
if [ ! -f "$DB" ]; then
  echo "No data yet. Run:  ./run.sh update"
  exit 1
fi
step "Building the website"
python scraper/normalize.py --db "$DB"
python scraper/normalize_websites.py --db "$DB"
python site/build_site.py --db "$DB" --out dist-private

# --- serve ------------------------------------------------------------------
step "Starting the website on http://localhost:$PORT"
if command -v lsof >/dev/null; then
  OLD=$(lsof -ti tcp:"$PORT" || true)
  [ -n "$OLD" ] && kill $OLD 2>/dev/null && sleep 1
fi
( sleep 1; command -v open >/dev/null && open "http://localhost:$PORT" ) &
echo "Leave this window open while you use the site. Press Ctrl+C to stop it."
python -m http.server -d dist-private "$PORT"
