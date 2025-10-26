# PropBot v0.1.1

Production-ready arbitrage runner with FastAPI, Binance Futures integration, SQLite
ledger, and the System Status web console. Release 0.1.1 ships the Telegram
control/alert bot, the SLO-aware status API (with automatic HOLD fail-safe), and
the `/api/ui/status/...` monitoring surface required for the v0.1.1 tag.

## Getting started

Two supported bootstrap paths are outlined below. Both assume the repository has
been cloned to `~/propbot`.

### Option A — Local macOS virtualenv (no Docker)

Create an isolated Python environment, install dependencies, run tests, and
start the API in safe paper mode:

```bash
/usr/bin/python3 -m venv ~/propbot/.venv
source ~/propbot/.venv/bin/activate
~/propbot/.venv/bin/pip install -U pip wheel
~/propbot/.venv/bin/pip install -r ~/propbot/requirements.txt
~/propbot/.venv/bin/pytest -q
cp ~/propbot/.env.example ~/propbot/.env
SAFE_MODE=true PROFILE=paper AUTH_ENABLED=true API_TOKEN=devtoken123 \
  ~/propbot/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --reload
```

Interactive docs remain available at `http://127.0.0.1:8000/docs`.

### Option B — Docker Compose (new workstation friendly)

Pull the v0.1.1 image from GHCR (or build locally), then bring the stack up via
Compose. The compose file consumes the `TAG` environment variable for image
selection.

```bash
export REPO=my-org
docker pull ghcr.io/${REPO}/propbot:v0.1.1
TAG=v0.1.1 docker compose pull
TAG=v0.1.1 docker compose up -d
curl -f http://127.0.0.1:8000/healthz
```

Makefile helpers mirror the same workflow:

```bash
export REPO=my-org
TAG=v0.1.1 make up
make curl-health
make logs
make down
```

Set `BUILD_LOCAL=1 make up` to rebuild the image on the fly instead of pulling
from GHCR. Runtime artefacts (ledger, runtime_state.json) are stored under
`./data` and persist between restarts.

## Environment configuration (`.env`)

Copy `.env.example` to `.env` and update the placeholders. Every variable in
`.env.example` is documented inline; the most important knobs are summarised
below:

- **Runtime profile & guards**
  - `PROFILE` — `paper`, `testnet`, or `live` broker profile.
  - `MODE` — descriptive deployment label used in metrics.
  - `SAFE_MODE` — when `true`, blocks live order placement (recommended
    default).
  - `DRY_RUN_ONLY` — forces the internal paper broker, regardless of profile.
  - `TWO_MAN_RULE` — require two-man approval before resuming trading.
  - `POST_ONLY`, `REDUCE_ONLY`, `ORDER_NOTIONAL_USDT`, `MAX_SLIPPAGE_BPS`,
    `MIN_SPREAD_BPS`, `POLL_INTERVAL_SEC`, `TAKER_FEE_BPS_*` — runtime loop
    controls.
  - `ENABLE_PLACE_TEST_ORDERS` — allow real order placement on testnet.
- **Risk limits**
  - `MAX_POSITION_USDT` and `MAX_POSITION_USDT__<SYMBOL>` — per-symbol notional
    caps.
  - `MAX_OPEN_ORDERS` and `MAX_OPEN_ORDERS__<venue>` — outstanding order caps.
  - `MAX_DAILY_LOSS_USDT` — absolute drawdown stop in USDT.
- **Auth & rate limiting**
  - `AUTH_ENABLED` + `API_TOKEN` — enable bearer auth for mutating routes.
  - `IDEM_TTL_SEC`, `API_RATE_PER_MIN`, `API_BURST` — idempotency and rate
    limit configuration.
- **Telegram control bot**
  - `TELEGRAM_ENABLE=true` to start the bot alongside FastAPI.
  - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` — credentials issued by
    BotFather.
  - `TELEGRAM_PUSH_MINUTES` — periodic status push interval (minutes).
- **Binance / OKX keys**
  - `BINANCE_UM_API_KEY_TESTNET` / `BINANCE_UM_API_SECRET_TESTNET` — Binance
    UM testnet credentials (`BINANCE_UM_BASE_TESTNET` override optional).
  - `BINANCE_LV_API_KEY` / `BINANCE_LV_API_SECRET` — Binance Futures live keys
    (`BINANCE_LV_BASE_URL` optional).
  - `OKX_API_KEY_TESTNET`, `OKX_API_SECRET_TESTNET`,
    `OKX_API_PASSPHRASE_TESTNET` — optional OKX testnet integration.

For live trading, populate the `BINANCE_LV_*` variables only in locked-down
profiles and keep `.env` outside version control.

## Monitoring & control API surface

System Status now exposes three operator-friendly endpoints:

- `GET /api/ui/status/overview` — aggregated view. Example:

  ```bash
  curl -s http://127.0.0.1:8000/api/ui/status/overview | jq '{overall, alerts}'
  ```

  The `overall` field reports `OK/WARN/ERROR/HOLD`. `alerts` enumerates active
  incidents with severity, human-readable message, and component references. Any
  critical SLO breach automatically drives the runtime into HOLD + SAFE_MODE.

- `GET /api/ui/state` — snapshot of runtime flags, exposures, ledger-derived
  orders/fills, loop status, and risk assessments. Secrets in the response are
  redacted as `***redacted***`.

- `PATCH /api/ui/control` — update runtime limits (min spread, slippage, dry
  run toggle, etc.) while in paper/testnet + SAFE_MODE. Include bearer auth when
  `AUTH_ENABLED=true`:

  ```bash
  curl -X PATCH http://127.0.0.1:8000/api/ui/control \
    -H "Authorization: Bearer $API_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"order_notional_usdt": 150, "min_spread_bps": 1.5}'
  ```

Related endpoints such as `GET /api/ui/events` continue to power the System
Status UI and now participate in the v0.1.1 Web/API panel.

## Telegram control & alerts

Enable the Telegram bot with the variables listed above. Once connected, the bot
sends periodic portfolio summaries (PnL, SAFE_MODE, profile, open positions) and
accepts the following commands from the authorised chat:

- `/pause` — enables SAFE_MODE and puts the loop into HOLD.
- `/resume` — disables SAFE_MODE and resumes the trading loop (respecting
  approvals/two-man rule).
- `/status` — returns the latest System Status summary on demand.
- `/close` or `/close_all` — triggers `cancel_all_orders` (only honoured for
  `PROFILE=testnet`).

Alerts are emitted for mode transitions, auto-HOLD triggers, and status push
failures. Every message avoids leaking API keys or bearer tokens.

## Safety reminder for Binance live

`PROFILE=live` with `SAFE_MODE=false` and valid `BINANCE_LV_*` keys will route
orders to real Binance Futures accounts. Double-check risk limits, Telegram
access, and two-man approvals before resuming trading in live mode. Never store
real credentials in repositories or unattended hosts.

## Release helpers

Use the updated Makefile target to tag releases in sync with Docker packaging:

```bash
make release TAG=0.1.1
```

This creates an annotated `v0.1.1` tag and pushes it to the configured remote,
triggering Docker Release workflows and compose smoke tests.
