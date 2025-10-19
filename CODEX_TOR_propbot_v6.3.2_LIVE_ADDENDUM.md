
# PropBot v6.3.2 — LIVE ENABLEMENT ADDENDUM (Spot Testnet + Live)
**Owner:** Denis • **Date:** <DATE>  
**Mode:** IMPLEMENTATION ONLY (no‑questions) — extend FULL/FULL_PLUS ToR to achieve **live trading capability** behind SAFE_MODE gates.

> This addendum upgrades scope from “paper/testnet baseline” to **real Spot trading** (Binance), keeping defaults SAFE.
> It must be executed after (or together with) the base ToR. If something is missing, create it. If conflicts occur, resolve and re‑push until Merge is enabled.

---

## A) Goal (What changes vs. base ToR)
- Implement **Binance Spot** integration with **Testnet** and **Live** profiles.
- Provide REST + (minimal) WS wiring required for order flow and basic health.
- Add `/api/live/*` endpoints and acceptance to place **/order/test** in SAFE_MODE and **real /order** when disabled.
- Enforce **guardrails**, **approvals**, and **preflight checks** before allowing live orders.
- Provide **docs** for key management (subaccounts, IP whitelist, “trade only; no withdrawals”), and **.env** handling.

> Live trading remains disabled by default (`SAFE_MODE=true`). Live activation requires explicit steps and passes preflight.

---

## B) New Config & Secrets

### B1. `.env` (not committed; validated by ASCII‑validator)
```
BINANCE_API_KEY_TESTNET=
BINANCE_API_SECRET_TESTNET=
BINANCE_API_KEY_LIVE=
BINANCE_API_SECRET_LIVE=
SAFE_MODE=true
EXCHANGE_PROFILE=paper   # allowed: paper|testnet|live
```

### B2. `configs/config.testnet.yaml` additions
```yaml
profile: testnet
exchange: binance_spot
routing:
  base_url_rest: "<BINANCE_TESTNET_REST_BASE>"      # set per official docs
  base_url_ws:   "<BINANCE_TESTNET_WS_BASE>"
safe_mode: true
approvals:
  two_man_rule: true
  require_token: true
preflight:
  time_skew_ms_max: 250
  ping_timeout_ms: 1500
  permissions_required: ["SPOT_TRADE"]
```

### B3. `configs/config.live.yaml` additions
```yaml
profile: live
exchange: binance_spot
routing:
  base_url_rest: "<BINANCE_LIVE_REST_BASE>"          # set per official docs
  base_url_ws:   "<BINANCE_LIVE_WS_BASE>"
safe_mode: true           # default true; must be flipped explicitly
approvals:
  two_man_rule: true
  require_token: true
preflight:
  time_skew_ms_max: 150
  ping_timeout_ms: 1000
  permissions_required: ["SPOT_TRADE"]
```

> NOTE: Use values from official exchange docs at implementation time; do not hard‑code if docs provide discovery or versioning.

---

## C) API — New `/api/live/*` Endpoints

### C1. GET `/api/live/ping`
- Calls exchange ping; returns `{ "ok": true, "serverTime": ..., "clock_skew_ms": ... }`.
- Acceptance: `200`, skew within `preflight.time_skew_ms_max` flips status OK/WARN.

### C2. GET `/api/live/account`
- Uses keys for current `EXCHANGE_PROFILE` (testnet/live).
- Returns balances and permissions summary, e.g. `{ "ok": true, "canTrade": true, "balances":[...] }`.
- Acceptance: `200`, `canTrade` present; if permissions missing ⇒ WARN/ERROR + HOLD (via Status).

### C3. POST `/api/live/order`
Body example:
```json
{
  "symbol": "BTCUSDT",
  "side": "BUY",
  "type": "LIMIT",
  "timeInForce": "GTC",
  "quantity": "0.001",
  "price": "25000.00"
}
```
Behavior:
- If `safe_mode=true` ⇒ call **test endpoint** (`/order/test`) and return `{ "ok": true, "mode": "test" }`.
- If `safe_mode=false` and **preflight & approvals** pass ⇒ call **real `/order`**, return order id/payload.
- If preflight fails ⇒ `409` with `{ "ok": false, "reason": "preflight_failed", "details": ... }` and set HOLD.

### C4. POST `/api/live/cancel`
- Cancel by `symbol`+`orderId` or `origClientOrderId`. Return `{ "ok": true }` or reason.

### C5. GET `/api/live/open_orders`
- List open orders for symbol or all.

> All `/api/live/*` must write audit trail to journal/outbox (even in SAFE_MODE).

---

## D) Exchange Adapter (Binance Spot)

### D1. Structure
```
app/exchanges/binance_spot.py
app/exchanges/__init__.py
```
- Class `BinanceSpotClient(profile: str, api_key: str, api_secret: str, base_urls: dict)`
- Methods: `ping()`, `server_time()`, `account()`, `order_test(payload)`, `order(payload)`, `cancel(params)`, `open_orders(params)`
- Error mapping → internal reason codes (e.g., RATE_LIMIT, INVALID_SYMBOL, INSUFFICIENT_BALANCE).

### D2. Signing & REST
- Use HMAC‑SHA256 query signing with `timestamp`/`recvWindow` as per official docs.
- Respect rate limits; surface hits into **Rate‑Limit Governor** status component.

### D3. WebSocket (minimal)
- Connect to aggregate trade or book ticker stream for a selected symbol; keep last `ts`/gap metric for SLO.
- On disconnect ⇒ flip connection component WARN/ERROR and trigger **cancel_on_disconnect** if enabled.

---

## E) Guardrails & Approvals (Live)

- **SAFE_MODE** default `true` ⇒ all orders routed to `/order/test`.
- **Two‑Man Rule** (mock is fine): require a short‑lived token (e.g., `approvals/token.txt`) signed by a second operator to flip SAFE_MODE for the session.
- **Preflight** before real orders:
  1) `/api/live/ping` OK and `clock_skew_ms` within threshold
  2) `account.canTrade == true`
  3) Permissions include `SPOT_TRADE`
  4) Maintenance window not active
  5) Risk caps leave sufficient headroom
- If any check fails ⇒ refuse order, set HOLD, log reason.

---

## F) Tests (upgrade)

- `tests/test_live_ping.py`: asserts `/api/live/ping` 200 and skew check logic.
- `tests/test_live_account.py`: **skip** if env keys missing; otherwise asserts fields and permissions mapping.
- `tests/test_live_order_safe.py`: with SAFE_MODE=true, `/api/live/order` calls `order_test`; returns `"mode":"test"`.
- `tests/test_live_order_real.py`: **skipped by default** unless `ALLOW_LIVE_ORDERS=1` in env and SAFE_MODE=false. Places very small order on a controlled symbol (configurable) and cancels it; asserts journal entries.
- `tests/test_ws_minimal.py`: with WS enabled and network available, asserts periodic ticks; otherwise **xpass/skip** with warning.

---

## G) CI Adjustments

- Add conditional matrix that runs live tests **only** when repo secrets are provided (e.g., `BINANCE_API_KEY_TESTNET`, etc.).
- Default CI path runs **paper/testnet mocks** and integration that doesn’t require secrets.
- Merge is blocked until **all non‑secret tests** are green; secret‑based jobs are optional and shown as “skipped” if no creds.

---

## H) Docs (live)

Update/add to `docs/`:
- **LIVE_TRADING_README.md** — how to set `.env`, subaccount creation, disable withdrawals, enable only “Spot/Margin trade”, add **IP whitelist**, place minimal test trade, how to flip SAFE_MODE with Two‑Man Rule token.
- **OPERATIONS_LIVE.md** — preflight checklist, how to watch SLO/Status, what to do on WARN/ERROR, rollback plan.
- **SECURITY.md** — secrets handling, `.gitignore`, rotating keys, principle of least privilege.

---

## I) Definition of Done (live)

- `/api/live/*` endpoints present and functional in testnet end‑to‑end.
- SAFE_MODE on by default; `/order` routes to `/order/test`.
- With proper env + explicit SAFE_MODE off + approvals, a **real** order can be placed and cancelled.
- Preflight and guardrails enforce HOLD/refusal on bad conditions.
- CI green; live jobs optionally run when secrets available.
- PR updated with evidence (logs), risks, rollback; Merge button enabled.

---

**End — LIVE ENABLEMENT ADDENDUM**
