#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

# Ensure venv is activated (created in Dockerfile)
if [ -d "/opt/venv" ]; then
  # shellcheck disable=SC1091
  source /opt/venv/bin/activate
fi

# Start aria2 in background; ignore failure
if command -v aria2c >/dev/null 2>&1; then
  aria2c --enable-rpc \
         --rpc-listen-all=false \
         --rpc-allow-origin-all \
         --daemon=true \
         --max-tries=50 \
         --retry-wait=3 \
         --continue=true \
         --min-split-size=4M \
         --split=10 \
         --allow-overwrite=true || true
  echo "[start.sh] aria2c started (background)"
else
  echo "[start.sh] WARNING: aria2c not found in PATH"
fi

# give aria2 a moment to come online
sleep 2

# Exec the bot (PID 1 will be python process)
exec python3 terabox.py
