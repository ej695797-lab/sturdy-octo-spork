#!/usr/bin/env bash
# Start the Yeeps 1.31.0-blaze server on this PC (Linux / macOS).
#   ./start.sh            run the server (keep this window open)
#   ./start.sh --tunnel   also open a free Cloudflare quick tunnel (https://....trycloudflare.com)
# First run: creates server/.venv, installs requirements, creates server/.env.
set -e
cd "$(dirname "$0")/server"
PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "Setting up Python packages (first run)..."
  "$PY" -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements.txt
fi
.venv/bin/python setup_env.py
PORT=$(grep -E '^PORT=' .env | cut -d= -f2 | tr -d '[:space:]'); PORT=${PORT:-8000}

if [ "$1" = "--tunnel" ]; then
  if ! command -v cloudflared >/dev/null; then
    echo "cloudflared isn't installed: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    exit 1
  fi
  cloudflared tunnel --url "http://127.0.0.1:$PORT" > tunnel.log 2>&1 &
  TUN=$!
  trap 'kill $TUN 2>/dev/null' EXIT
  for _ in $(seq 1 30); do
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log | head -n 1)
    [ -n "$URL" ] && break
    sleep 1
  done
  echo "================================================================"
  echo " Tunnel: ${URL:-(not ready, see server/tunnel.log)}"
  echo " Quick-tunnel addresses change every time. Put this one on test"
  echo " headsets with the override file (see README), or use a named"
  echo " tunnel with your own domain for an address that never changes."
  echo "================================================================"
fi

echo "Server on port $PORT (Ctrl+C to stop). Editor: http://127.0.0.1:$PORT/editor"
exec .venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
