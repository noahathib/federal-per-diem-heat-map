#!/bin/zsh
# Double-click this file in Finder to start the per diem dashboard.
# It opens your browser, keeps the Mac awake while it runs, and stops
# when you press Ctrl-C or close the Terminal window.
#
# Override the defaults if you want:
#   PER_DIEM_HOST=0.0.0.0 PER_DIEM_PORT=9000 ./scripts/start_dashboard.command

set -e
cd "${0:A:h}/.."

# 0.0.0.0 binds every interface, so the dashboard answers on this Mac's LAN
# address (10.9.4.85 today) as well as on localhost. Binding 0.0.0.0 rather
# than the literal IP means DHCP handing out a new address doesn't stop the
# server from starting.
HOST="${PER_DIEM_HOST:-0.0.0.0}"
PORT="${PER_DIEM_PORT:-8765}"

if [[ ! -x .venv/bin/federal-per-diem-dashboard ]]; then
  echo "The virtualenv is missing. Build it once with:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -e ."
  exit 1
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Something is already listening on port $PORT:"
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN
  echo
  echo "Open http://$HOST:$PORT/ to use it, or stop it first with:"
  echo "  kill \$(lsof -t -nP -iTCP:$PORT -sTCP:LISTEN)"
  exit 1
fi

if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" ]]; then
  echo "Network callers will be asked for the dashboard password."
  echo "Change it any time with:  .venv/bin/federal-per-diem-dashboard --set-password"
  echo
fi

echo "Starting the dashboard on http://$HOST:$PORT/ — press Ctrl-C to stop."
exec caffeinate -is .venv/bin/federal-per-diem-dashboard \
  --host "$HOST" --port "$PORT" --open --verbose
