# Order Lifecycle Hardening

## State Machine Overview

Order lifecycle events are normalised through `app.execution.order_state`. The
state machine exposes the following canonical statuses:

* `NEW`
* `PENDING`
* `ACK`
* `PARTIAL`
* `FILLED`
* `CANCELED`
* `REJECTED`
* `EXPIRED`

`apply_exchange_update` accepts the current local state together with an
exchange payload and returns an updated snapshot. Transitions are idempotent:
replaying the same ACK or FILLED update does not mutate the state or inflate the
filled quantity. Partial fills are detected via cumulative quantities or
per-fill deltas.

## Client Order Identifier Idempotency

The helper `app.router.adapter.generate_client_order_id` generates a stable
`clientOrderId` for the router. The identifier is a hash of the normalised
`strategy`, `venue`, `symbol`, `side`, timestamp bucket and a deterministic
nonce (typically the intent id). Retries for the same logical order therefore
re-use the same identifier which lets the venue deduplicate submissions.

## Duplicate Fill Handling

`apply_exchange_update` tracks both the cumulative filled quantity and the last
`fill_id`. When a payload replays the same fill, the function keeps the filled
quantity unchanged and reports an event flag. The router logs
`event="duplicate_fill_ignored"` when such a replay is observed, ensuring PnL
and position accounting remain stable.

