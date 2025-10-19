#!/usr/bin/env bash
set -euo pipefail
APP=/opt/crypto-bot
TS=$(date +%Y%m%d-%H%M%S)
REL=$APP/releases/$TS
mkdir -p "$REL"
cp -r . "$REL/"
ln -sfn "$REL" "$APP/current"
systemctl restart crypto-bot@9000 || true
echo "Deployed canary at $REL"
