#!/usr/bin/env bash
set -euo pipefail
APP=/opt/crypto-bot
# expects previous release exists
PREV=$(ls -1d $APP/releases/* | tail -n 2 | head -n 1)
[ -z "$PREV" ] && { echo "No previous release"; exit 1; }
ln -sfn "$PREV" "$APP/current"
systemctl restart crypto-bot || true
echo "Rolled back to $PREV"
