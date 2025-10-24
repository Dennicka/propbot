# Testnet Quickstart

This addendum highlights runtime controls that can now be updated without restarts while running the dashboard against paper or testnet environments.

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

### Run from the published image

Set `REPO` to the GitHub organisation/user that owns the GHCR namespace (`ghcr.io/<REPO>/propbot:<TAG>`). `TAG` defaults to `main`, but you can point it to a release or commit tag:

```bash
export REPO=my-org
docker compose up -d           # pulls ghcr.io/$REPO/propbot:main by default
docker compose logs -f app
```

To start a specific published release without using Make targets, set the tag explicitly:

```bash
export REPO=my-org
TAG=v0.1.0 docker compose -f docker-compose.yml up -d
```

Make targets accept the same variables and keep the long-running helpers available:

```bash
export REPO=my-org
make up
make curl-health    # GET /healthz (expects HTTP 200)
make logs           # follow container logs
make down           # stop and remove the compose stack
```

Compose mounts the local `./data` directory into the container as `/app/data`, so files such as `runtime_state.json` and `ledger.db` persist across restarts. If auth is enabled, provide `API_TOKEN` via `.env` or the shell before invoking compose.

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

Use the Make target to create and push annotated release tags:

```bash
make release TAG=0.1.1
```

Tags are pushed to `origin` by default; provide `REMOTE=...` to override the remote.

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
