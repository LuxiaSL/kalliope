#!/usr/bin/env bash
# kalliope-enrich — one command to bring catalog enrichment up to date.
# Runs the analyzer then the genre backfill, both incremental: already-
# covered tracks are skipped, so this is cheap to run whenever new music
# lands (and the station runs it on a timer — see KALLIOPE_ENRICH_HOURS).
#
# Usage: enrich.sh            # everything new
#        enrich.sh --redo     # forwarded to both scripts: redo everything
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

# .env for CATALOG_DB / ANTHROPIC_API_KEY (genre inference); optional
if [ -f .env ]; then set -a; . ./.env; set +a; fi

rc=0
echo "── analyze ──"
nice -n 19 uv run scripts/analyze.py "$@" || rc=$?
echo "── genres ──"
nice -n 19 uv run scripts/genres.py "$@" || rc=$?
exit $rc
