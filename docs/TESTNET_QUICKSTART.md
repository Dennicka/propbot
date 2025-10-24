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
