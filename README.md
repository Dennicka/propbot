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
from GHCR. Runtime artefacts (`runtime_state.json`, the SQLite ledger, incident
exports) are stored under `./data` and persist between restarts.

 codex/add-operator-runbook-documentation-30d5c6
### 🚀 Production deployment on Linux

1. Provision a clean Linux host with Docker Engine and the Compose plugin.
2. Clone the repository to `/opt/propbot` (or similar) and `cd /opt/propbot/deploy`.
3. Create the persistent data directory **before** starting the container and grant
   write access to the container user (UID 1000 in the default image):
   ```bash
   sudo mkdir -p /opt/propbot/data
   sudo chown 1000:1000 /opt/propbot/data
   sudo chmod 770 /opt/propbot/data
   ```
   The directory is mounted as `/app/data` and must remain writable so
   `runtime_state.json`, `ledger.db`, exports, and checkpoints survive restarts.
4. Copy `deploy/env.example.prod` to `.env`, then fill in API keys, `PROFILE`,
   `SAFE_MODE`, `DRY_RUN_ONLY`, Telegram settings, risk limits, and the bearer
   `API_TOKEN` (never commit secrets to git).
5. Keep the bot paused on first boot: `SAFE_MODE=true`, `DRY_RUN_ONLY=true` (for
   paper/testnet) or leave `SAFE_MODE=true` and plan to send `mode=HOLD` via
   Telegram/CLI in live environments.
6. Start the stack: `docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d`.
7. Validate the instance with Swagger (`https://<host>/docs`) and run `python3
   cli/propbotctl.py --base-url https://<host> status` to confirm the bot stays in
   HOLD.
8. After manual checks (balances, limits, `loop_pair`/`loop_venues`, approvals),
   resume trading via Telegram or `python3 cli/propbotctl.py --base-url
   https://<host> --token "$API_TOKEN" resume`.


 main
### Права на каталог `data`

Перед запуском production-контура через `docker-compose.prod.yml` создайте на
сервере каталог `./data` рядом с compose-файлом и убедитесь, что он доступен на
запись пользователю, от которого запускается Docker (например, `sudo mkdir -p
./data && sudo chown 1000:1000 ./data && sudo chmod 770 ./data`). Этот каталог
монтируется в контейнер как `/app/data` и содержит постоянные базы/состояние.
Права должны позволять контейнеру читать и записывать файлы, иначе сервис не
сможет стартовать.

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
  - `LOOP_PAIR` / `LOOP_VENUES` — optional overrides for the live loop symbol
    and venue list (uppercase symbol, comma-separated venues). When unset the
    loop follows strategy defaults.
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
  - `BINANCE_LV_API_KEY_TESTNET` / `BINANCE_LV_API_SECRET_TESTNET` — optional
    segregated credentials when running live and testnet bots in parallel.
  - `OKX_API_KEY_TESTNET`, `OKX_API_SECRET_TESTNET`,
    `OKX_API_PASSPHRASE_TESTNET` — optional OKX testnet integration.

For live trading, populate the `BINANCE_LV_*` variables only in locked-down
profiles and keep `.env` outside version control.

## Safety reminder for Binance live

`PROFILE=live` with `SAFE_MODE=false` **and** `DRY_RUN_ONLY=false` plus valid
`BINANCE_LV_*` keys will route orders to real Binance Futures accounts. Keep the
bot in HOLD and `SAFE_MODE=true` on startup, double-check risk limits,
`loop_pair`/`loop_venues`, balances, Telegram access, and two-man approvals
before resuming trading in live mode. Never store real credentials in
repositories or unattended hosts.

For routine operational procedures (health checks, HOLD management, secret
rotation, exports, safe restarts) see `docs/OPERATOR_RUNBOOK.md`. Оператор может
пользоваться Telegram-ботом или локальным `propbotctl` (CLI требует локального
или SSH-доступа к хосту и bearer-токен).

## CLI `propbotctl`

The repository ships a thin operator CLI for frequently used status checks and
controls. Run it with the local interpreter (requires the `requests`
dependency):

```bash
codex/add-operator-runbook-documentation-30d5c6
python3 cli/propbotctl.py --base-url https://<host> status
python3 cli/propbotctl.py --base-url https://<host> components

python3 cli/propbotctl.py status
python3 cli/propbotctl.py components
 main
```

Mutating commands require a bearer token that has access to `/api/ui/control`
and `/api/ui/secret`. Pass it explicitly via `--token` or set it through the
`API_TOKEN` environment variable prior to invoking the command. **Never commit
tokens or secrets to git.**

```bash
# Pause and resume trading from the terminal
codex/add-operator-runbook-documentation-30d5c6
python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" pause
python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" resume

# Rotate the Binance live secret
python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" rotate-key --value 'new-secret'

# Export recent events to a JSON file
python3 cli/propbotctl.py --base-url https://<host> export-log --out ./events_export.json
```


python3 cli/propbotctl.py --token "$API_TOKEN" pause
python3 cli/propbotctl.py --token "$API_TOKEN" resume

# Rotate the Binance live secret
python3 cli/propbotctl.py --token "$API_TOKEN" rotate-key --value 'new-secret'

# Export recent events to a JSON file
python3 cli/propbotctl.py export-log --out ./events_export.json
```

 main
## Release helpers

Use the updated Makefile target to tag releases in sync with Docker packaging:

```bash
make release TAG=0.1.1
```

This creates an annotated `v0.1.1` tag and pushes it to the configured remote,
triggering Docker Release workflows and compose smoke tests.
