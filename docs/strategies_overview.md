# Strategies overview

## StrategyRegistry & StrategyInfo
The `StrategyRegistry` keeps an in-memory catalogue of all strategies available
to the trading system. Each entry is a `StrategyInfo` record describing the
human-readable metadata (identifier, name, description, tags) as well as
operational guardrails. Budgets define notional, daily loss, and open position
caps (`max_notional_usd`, `max_daily_loss_usd`, `max_open_positions`). Lifecycle
flags cover `enabled`, the routing `mode` (sandbox/canary/live), and the
relative `priority` used by routers.

## Budgets & risk integration
`check_strategy_budget(...)` is called by the routing layer before orders are
forwarded to execution. The check ensures the strategy stays within its defined
notional, daily loss, and open position limits. These strategy-specific checks
complement the global `RiskGovernor` and provide an additional guardrail that
can fail fast on obviously invalid requests.

## Lifecycle guard
`check_strategy_lifecycle(...)` validates the strategy status before trades are
sent. It verifies that the strategy is enabled, checks the routing mode, and
enforces profile-specific policies. Sandbox strategies are blocked when the bot
runs in a live profile, while paper/test profiles allow all strategies. The
returned decision records the effective mode and priority so that the router can
make deterministic choices.

## UI integration
The UI surfaces strategy data through the following endpoints:

- `/api/ui/execution` — includes the `strategy_id` for orders and execution
  plans.
- `/api/ui/pnl` — exposes a `by_strategy` section with per-strategy PnL
  snapshots.
- `/api/ui/strategy-metrics` — returns `StrategyPerformance` objects containing
  win rate, average PnL, drawdown, and trade counts.
- `/api/ui/config` and `/api/ui/status` — list registered strategies including
  budgets and lifecycle metadata.

## How to add a new strategy
1. Extend `register_default_strategies()` (or the relevant bootstrap hook) with
   a new `StrategyInfo` entry.
2. Define the desired budget values and lifecycle settings (mode, priority,
   enabled flag).
3. Ensure routers map the new strategy to the appropriate `strategy_id` when
   constructing orders or plans.
4. Verify that the strategy produces PnL and performance data so it appears in
   `/api/ui/pnl` and `/api/ui/strategy-metrics`.
5. Confirm the configuration endpoints list the strategy with its metadata and
   limits.
