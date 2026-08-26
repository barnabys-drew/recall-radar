#!/bin/bash
# ---------------------------------------------------------------------------
# recall-radar launcher
#
#   ./start.sh            sync the latest recalls, then open the dashboard
#   ./start.sh --fast     skip the sync, open cached data immediately
#   ./start.sh --no-open  start the server but don't launch a browser
#
# Creates ./venv on first run. The database lives outside the repo, in
# ~/.local/share/recall-radar/ (override with $RECALL_RADAR_DB).
# ---------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-5055}"
URL="http://localhost:${PORT}"
FAST=0
OPEN=1
for arg in "$@"; do
    case "$arg" in
        --fast|--no-sync) FAST=1 ;;
        --no-open)        OPEN=0 ;;
        *) echo "unknown flag: $arg"; exit 2 ;;
    esac
done

# --- 1. venv ---------------------------------------------------------------
if [ ! -x venv/bin/python ]; then
    echo "Creating venv..."
    python3 -m venv venv || { echo "  could not create venv"; exit 1; }
    ./venv/bin/pip install -q --upgrade pip
    ./venv/bin/pip install -q flask || { echo "  could not install flask"; exit 1; }
fi
PY=./venv/bin/python

# --- 2. refresh recall data ------------------------------------------------
if [ "$FAST" -eq 1 ]; then
    echo "Fast mode - showing cached recalls."
else
    echo "Syncing recalls..."
    # A source failing is not fatal: the dashboard is still useful with
    # whichever agency did respond, and the header shows how current each is.
    $PY -m recall_radar sync || echo "  sync reported a problem - serving cached data."
fi

# --- 3. stop anything already on the port ----------------------------------
if command -v lsof >/dev/null 2>&1 && lsof -ti:"$PORT" >/dev/null 2>&1; then
    echo "Stopping existing server on :${PORT}..."
    lsof -ti:"$PORT" | xargs -r kill
    sleep 1
fi

# --- 4. serve --------------------------------------------------------------
echo "Starting dashboard..."
PORT="$PORT" $PY app.py >/tmp/recall-radar-server.log 2>&1 &
APP_PID=$!
trap 'kill $APP_PID 2>/dev/null' INT TERM EXIT

curl -s --retry 25 --retry-delay 1 --retry-connrefused --connect-timeout 1 \
     --max-time 30 "$URL/api/stats" >/dev/null 2>&1

if [ "$OPEN" -eq 1 ]; then
    # WSL first (Windows browser), then Linux, then macOS.
    if command -v wslview >/dev/null 2>&1;      then wslview "$URL" >/dev/null 2>&1 &
    elif command -v xdg-open >/dev/null 2>&1;   then xdg-open "$URL" >/dev/null 2>&1 &
    elif command -v open >/dev/null 2>&1;       then open "$URL" >/dev/null 2>&1 &
    else echo "  Open $URL in your browser."; fi
fi

echo ""
echo "  Dashboard live at $URL"
echo "  Ctrl-C to stop."
echo ""
wait $APP_PID
