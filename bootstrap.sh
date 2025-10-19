#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip wheel
pip install -r requirements.txt
mkdir -p data logs
python -m alembic upgrade head || true
pytest -q || true
echo "Run: APP_ENV=local DEFAULT_PROFILE=paper .venv/bin/uvicorn app.server_ws:app --host 127.0.0.1 --port 8000"
