#!/usr/bin/env bash
set -euo pipefail
APP=crypto-bot
BASE=/opt/$APP
PREV=$(ls -1dt $BASE/releases/* | sed -n '2p')
[ -n "$PREV" ] || { echo "No previous release"; exit 1; }
ln -sfn "$PREV" "$BASE/current"
systemctl restart crypto-bot
echo "[OK] Rolled back to $PREV"
