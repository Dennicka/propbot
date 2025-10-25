# Testnet Quickstart

This addendum highlights runtime controls that can now be updated without restarts while running the dashboard against paper or testnet environments.

## Local macOS bootstrap (no Docker)

Use the following commands on the workstation `/Users/denis/propbot` to create a virtual environment, install dependencies, run tests, and start the API in paper mode with safe guards enabled.

```bash
/usr/bin/python3 -m venv /Users/denis/propbot/.venv
source /Users/denis/propbot/.venv/bin/activate
/Users/denis/propbot/.venv/bin/pip install -U pip wheel
/Users/denis/propbot/.venv/bin/pip install -r /Users/denis/propbot/requirements.txt
/Users/denis/propbot/.venv/bin/pytest -q
SAFE_MODE=true PROFILE=paper AUTH_ENABLED=true API_TOKEN=devtoken123 \
  /Users/denis/propbot/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --reload
```

Copy `.env.example` to `.env` when overrides are required: `cp /Users/denis/propbot/.env.example /Users/denis/propbot/.env`. The interactive docs are available at `http://127.0.0.1:8000/docs`.

## Binance Futures Testnet bootstrap

1. Скопируйте `.env.example` в `.env` и задайте переменные:

   ```dotenv
   PROFILE=testnet
   SAFE_MODE=true
   BINANCE_UM_API_KEY_TESTNET=your_testnet_key
   BINANCE_UM_API_SECRET_TESTNET=your_testnet_secret
   BINANCE_UM_BASE_TESTNET=https://testnet.binancefuture.com
   ```

2. Запустите сервис в тестнет-режиме (SAFE_MODE=true блокирует реальные ордера, но позволяет читать баланс/позиции):

   ```bash
   SAFE_MODE=true PROFILE=testnet AUTH_ENABLED=true API_TOKEN=devtoken123 \
     /Users/denis/propbot/.venv/bin/uvicorn app.main:app \
     --host 127.0.0.1 --port 8000 --reload
   ```

3. Откройте `http://localhost:8000` и убедитесь, что разделы **Balances** и **Exposures** показывают данные Binance Testnet.

4. Для вызова защищённых эндпоинтов используйте bearer-токен. Пример dry-run запроса (SAFE_MODE=true вернёт сообщение о пропуске ордера):

   ```bash
   curl -X POST http://localhost:8000/api/arb/execute \
     -H "Authorization: Bearer devtoken123" \
     -H "Content-Type: application/json" \
     --data '{"symbol":"BTCUSDT","side":"BUY","qty":0.001}'
   ```

   Если отключить `SAFE_MODE`, снять `DRY_RUN_ONLY` и выставить `ENABLE_PLACE_TEST_ORDERS=1`, ответ будет содержать результат реального размещения на тестнете.

### Binance Futures profiles

| Scenario | Required variables |
| --- | --- |
| Paper simulation | `PROFILE=paper`, `SAFE_MODE=true` — no real orders are submitted. |
| Binance Futures Testnet | `PROFILE=testnet`, `SAFE_MODE=true` (read-only by default), `BINANCE_UM_API_KEY_TESTNET`, `BINANCE_UM_API_SECRET_TESTNET`, optional `BINANCE_UM_BASE_TESTNET`.
| Binance Futures Live | `PROFILE=live`, `SAFE_MODE=false` (intentional), `BINANCE_LV_API_KEY`, `BINANCE_LV_API_SECRET`, optional `BINANCE_LV_BASE_URL`.

> SAFE_MODE=true + PROFILE=paper гарантируют, что реальные ордера не покидают сервис — используется встроенный симулятор.

## Runtime control patch API

The dashboard issues `PATCH /api/ui/control` requests when the **Edit Config** modal is submitted. You can also invoke it directly:

```bash
curl -X PATCH http://localhost:8000/api/ui/control \
  -H 'Content-Type: application/json' \
  -d '{
        "order_notional_usdt": 150,
        "min_spread_bps": 1.5,
        "max_slippage_bps": 8,
        "loop_pair": "ETHUSDT",
        "loop_venues": ["binance-um", "okx-perp"],
        "dry_run_only": true
      }'
```

When mutating endpoints need to be locked down, export a shared token before starting the API:

```bash
export AUTH_ENABLED=true
export API_TOKEN="super-secret-token"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Every `POST`/`PATCH` request must then include the bearer header:

```bash
curl -X PATCH http://localhost:8000/api/ui/control \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "order_notional_usdt": 150,
        "min_spread_bps": 1.5,
        "max_slippage_bps": 8,
        "loop_pair": "ETHUSDT",
        "loop_venues": ["binance-um", "okx-perp"],
        "dry_run_only": true
      }'
```

The helper CLI accepts the same token via `--api-token` (falls back to `API_TOKEN`) and forwards it to mutating endpoints when those commands are added:

```bash
python -m api_cli events --base-url http://localhost:8000 --api-token "$API_TOKEN"
```

## Deploy with Docker/Compose

### Run from the published GHCR image

Pull the published `v0.1.0` image from GHCR, then rely on the same tag for compose services and health checks.

```bash
export REPO=my-org
docker pull ghcr.io/${REPO}/propbot:v0.1.0
TAG=v0.1.0 docker compose pull
TAG=v0.1.0 docker compose up -d
docker compose ps
curl -f http://127.0.0.1:8000/healthz
curl -f http://127.0.0.1:8000/docs | head -n 20
```

The compose file mounts `./data` into `/app/data`, so ledgers and runtime state persist across restarts. Override environment variables (including `SAFE_MODE`, `PROFILE`, `AUTH_ENABLED`, `API_TOKEN`, and `BINANCE_*` keys) via `.env` or the shell prior to running compose.

Make targets wrap the same workflow:

```bash
export REPO=my-org
TAG=v0.1.0 make up
make curl-health    # GET /healthz (expects HTTP 200)
make logs           # follow container logs
make down           # stop and remove the stack
```

For a lightweight smoke test without compose, run:

```bash
IMAGE=ghcr.io/${REPO}/propbot:v0.1.0 make docker-run-image
```

SAFE_MODE defaults to `true`, so no real orders are emitted unless you deliberately set `PROFILE=live` and disable the guard with real Binance credentials.

### Build locally when required

Flip `BUILD_LOCAL=1` to force a local build (default image tag: `propbot:local`). Compose skips pulling from GHCR and rebuilds the image before starting the stack:

```bash
BUILD_LOCAL=1 make up
BUILD_LOCAL=1 make down
IMAGE=propbot:test make docker-build   # manual build with a custom tag
```

### GitHub Actions smoke test

The **Compose smoke test** workflow ensures the published image boots and serves `/docs` together with `/api/ui/state`. It runs automatically whenever a release is published and can be dispatched manually with an optional `tag` input (defaults to `latest`).

### Release helper target

Use the Make target to create and push annotated release tags that trigger the Docker Release workflow (`v*` semantics):

```bash
make release TAG=0.1.0
```

Tags are pushed to `origin` by default; set `REMOTE=upstream` (or similar) to override the destination remote.

Constraints:

- Available only when `ENV`/`PROFILE` is `paper` or `testnet`.
- `SAFE_MODE` must remain `true`; otherwise the API returns `403`.
- Unknown fields are ignored; values are normalised (floats/ints/bools) before applying.
- `max_slippage_bps` is clamped to `[0, 50]`, `min_spread_bps` to `[0, 100]`, and `order_notional_usdt` to `[1, 1_000_000]`.
- Fields set to `null` are skipped instead of raising server errors.
- The response contains the updated control block and a `changes` map with applied keys.
- Every successful patch persists the control snapshot to `data/runtime_state.json`; the runtime reloads this file on restart.

The dashboard also exposes `GET /api/ui/events` for paginating the event log. You can combine `offset`/`limit` (≤1000) with filters (`venue`, `symbol`, `level`, `search`) and optional `since`/`until` timestamps (window ≤7 days).
Exports are available through `GET /api/ui/events/export?format=csv|json` and `GET /api/ui/portfolio/export?format=csv|json`.

## Risk overview endpoint

`GET /api/risk/state` exposes the aggregated risk snapshot used by the dashboard (positions in USDT, per-venue exposure totals, current counters and limit breaches). This is useful for external monitoring or alerting without parsing the full UI payload.

```bash
curl http://localhost:8000/api/risk/state | jq
```

## Dashboard shortcuts

- **Positions** tab now lists every venue/symbol with per-row **Close** buttons (calls `POST /api/ui/close_exposure`).
- **Cancel All** buttons appear per venue on testnet once open orders are detected; they call `POST /api/ui/cancel_all` with a JSON `{ "venue": "binance-um" }` payload.
- **Events** card features level badges, filters (venue / level / message search), the overall event count, a **Download CSV** shortcut, and a **Load more** button that streams older entries via `/api/ui/events`.
- **Runtime Flags** card now shows the normalised control snapshot (post-PATCH values).
- **Exposures** table includes a `venue_type` column, while the Balances table ends with the aggregated USDT total.

These additions streamline intraday testing on Binance UM / OKX perpetual testnets without restarting the service.
