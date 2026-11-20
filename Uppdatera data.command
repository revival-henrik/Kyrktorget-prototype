#!/bin/bash
# Double-click in Finder: fetches new data (can take hours), then rebuilds and opens the site.
cd "$(dirname "$0")"
./run.sh update
