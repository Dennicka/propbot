# PnL and exposure overview

## Data sources
PnL and exposure views combine multiple data feeds: executed trades recorded in
the ledger, open positions from the portfolio state, and market data providing
mark prices. These inputs are normalised before powering UI payloads.

## PnL models
Position-level PnL is represented by `PositionPnlSnapshot` with realized and
unrealized profit components, fees, funding, and notional values. Portfolio
aggregations use `PortfolioPnlSnapshot` to combine those fields and expose a
net/gross PnL breakdown. Strategy-level aggregation is handled via
`StrategyPnlSnapshot`, which groups positions by `strategy_id` for the UI.

## Exposure models
Exposure payloads extend the PnL view with position size insights. The
`StrategyExposureSnapshot` (and related structures) track notional, net
quantities, and open position counts per strategy to explain risk concentration.

## UI endpoints
- `/api/ui/pnl` — returns portfolio totals, per-position PnL, and the
  `by_strategy` collection introduced in P3.6.
- `/api/ui/exposure` — exposes per-venue exposure details alongside the total
  net exposure figure for quick monitoring.
- `/api/ui/strategy-metrics` — ties recent trades to PnL to compute performance
  metrics such as win rate, average trade PnL, and drawdown.

## Tests & contracts
The following suites cover the PnL/exposure surface:

- `tests/pnl/` — snapshot and aggregation logic.
- `tests/exposure/` — exposure aggregators and helpers.
- `tests/ui/test_pnl_*` — FastAPI contracts for the PnL endpoints.
- `tests/ui/test_exposure_*` — UI exposure contract expectations.
- `tests/ui/test_strategy_metrics_endpoint.py` — ensures strategy metrics stay
  compatible with UI clients.

Updating models or payload shapes should be accompanied by test adjustments to
keep the UI contract stable.
