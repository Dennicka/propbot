# Testnet Quickstart v0.1.1

The v0.1.1 release introduces the System Status API (with automatic HOLD
fail-safe), the Telegram control/alert bot, and the `/api/ui/status/...` panel.
This addendum walks through bootstrapping PropBot against paper or Binance UM
Testnet profiles.

## Option A — Local macOS bootstrap (no Docker)

The commands below match a macOS workstation at `/Users/denis/propbot` and start
PropBot in paper mode with SAFE_MODE enabled.

```bash
/usr/bin/python3 -m venv /Users/denis/propbot/.venv
source /Users/denis/propbot/.venv/bin/activate
/Users/denis/propbot/.venv/bin/pip install -U pip wheel
/Users/denis/propbot/.venv/bin/pip install -r /Users/denis/propbot/requirements.txt
/Users/denis/propbot/.venv/bin/pytest -q
cp /Users/denis/propbot/.env.example /Users/denis/propbot/.env
SAFE_MODE=true PROFILE=paper AUTH_ENABLED=true API_TOKEN=devtoken123 \
  /Users/denis/propbot/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --reload
```

Docs and the web UI are available at `http://127.0.0.1:8000/docs` and
`http://127.0.0.1:8000/` after startup.

## Option B — Docker / Compose

Compose consumes the `TAG` variable to select the container image. Pull the
published GHCR image and start the stack:

```bash
export REPO=my-org
docker pull ghcr.io/${REPO}/propbot:v0.1.1
TAG=v0.1.1 docker compose pull
TAG=v0.1.1 docker compose up -d
curl -s http://127.0.0.1:8000/api/ui/status/overview | jq '{overall, alerts}'
```

Use `TAG=v0.1.1 make up` and `make down` for the Makefile wrappers, or set
`BUILD_LOCAL=1 make up` to rebuild the image locally.

## Environment variables

Copy `.env.example` to `.env` and fill in the placeholders. Highlights:

- `PROFILE=testnet`, `SAFE_MODE=true` — default paper-safe mode. Disable
  SAFE_MODE only when testnet order placement is required.
- `BINANCE_UM_API_KEY_TESTNET` / `BINANCE_UM_API_SECRET_TESTNET` — Binance UM
  testnet API credentials. Override the base URL via `BINANCE_UM_BASE_TESTNET`
  if needed.
- `ENABLE_PLACE_TEST_ORDERS=true` — required to submit orders to Binance UM
  testnet (still honouring SAFE_MODE unless disabled).
- `AUTH_ENABLED=true` + `API_TOKEN=<token>` — enables bearer auth for
  mutating endpoints (`PATCH /api/ui/control`, `POST /api/ui/arb/*`, etc.).
- Risk caps via `MAX_POSITION_USDT`, `MAX_POSITION_USDT__BTCUSDT`,
  `MAX_OPEN_ORDERS`, and `MAX_DAILY_LOSS_USDT`.
- Telegram bot variables (`TELEGRAM_ENABLE`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `TELEGRAM_PUSH_MINUTES`) for control and alerts.

Review `.env.example` for the full list, including live Binance placeholders.
Secrets are never checked into the repository.

## System Status API & SLO auto-HOLD

Query the new endpoints to verify the runtime state:

```bash
curl -s http://127.0.0.1:8000/api/ui/status/overview | jq '{overall, alerts}'
curl -s http://127.0.0.1:8000/api/ui/state | jq '.flags + {risk_blocked, risk_reasons}'
```

`overall` reports the aggregate health (`OK/WARN/ERROR/HOLD`). When a critical
SLO is breached (for example, recon mismatch or persistent latency breach), the
runtime automatically flips into HOLD, enforces SAFE_MODE, and stops the
loop. All secrets in the payload are redacted as `***redacted***`.

Mutate runtime parameters via `PATCH /api/ui/control` while running paper or
testnet with SAFE_MODE enabled:

```bash
curl -X PATCH http://127.0.0.1:8000/api/ui/control \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"order_notional_usdt": 75, "min_spread_bps": 1.0, "dry_run_only": true}'
```

`GET /api/ui/events` keeps powering the dashboard and the `/api/ui/events/export`
utility.

## Telegram control bot

Enable the Telegram bot by exporting `TELEGRAM_ENABLE=true`, the bot token, and
an authorised chat ID. Once running, the bot:

- pushes status snapshots (PnL, profile, SAFE_MODE, open positions, risk
  breaches) every `TELEGRAM_PUSH_MINUTES`,
- accepts `/pause`, `/resume`, `/status`, and `/close` (`/close_all`) commands
  from the authorised chat,
- redacts secrets from every outgoing message.

The `/close` command triggers `cancel_all_orders` and is only honoured while the
profile is set to `testnet`.

## Binance safety note

When switching to `PROFILE=live`, remember that production keys in
`BINANCE_LV_API_KEY` / `BINANCE_LV_API_SECRET` unlock real funds. Keep
`SAFE_MODE=true` until manual approval, verify two-man acknowledgements, and
monitor the System Status overview for HOLD/critical alerts before resuming.
