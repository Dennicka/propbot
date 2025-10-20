#!/usr/bin/env bash
set -euo pipefail
APP=crypto-bot
BASE=/opt/$APP
TS=$(date +%Y%m%d_%H%M%S)
NEW=$BASE/releases/$TS
mkdir -p "$NEW"
rsync -a --delete ./ "$NEW/"
python3 -m venv "$NEW/.venv"
. "$NEW/.venv/bin/activate"
pip install -r "$NEW/requirements.txt"
( uvicorn app.server_ws:app --host 127.0.0.1 --port 8000 & echo $! > "$NEW/uv.pid"; )
sleep 2
curl -fsS http://127.0.0.1:8000/api/health >/dev/null
curl -fsS http://127.0.0.1:8000/live-readiness >/dev/null
kill "$(cat "$NEW/uv.pid")"
ln -sfn "$NEW" "$BASE/current"
systemctl restart crypto-bot
echo "[OK] Release switched to $NEW"
