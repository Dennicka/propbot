
# PropBot v6.3.2 — DERIVATIVES ARBITRAGE ADDENDUM (Binance UM Futures ↔ OKX Perps)
**Owner:** Denis • **Date:** 2025-10-19 15:44 UTC  
**Mode:** IMPLEMENTATION ONLY (no‑questions). Extend FULL/FULL_PLUS ToR to implement a **cross‑venue long/short arbitrage engine** on **derivatives only** (NO spot).

> Goal: be able to run **paper/testnet** and — with explicit operator approvals — **live** hedged trades: e.g., Long on Binance UM Futures, Short on OKX Perps (or vice‑versa) when **net edge after all costs** is positive. All guardrails remain enforced; SAFE_MODE defaults ON.

---

## A) Scope & Requirements

- Venues: **Binance Futures UM** (USD‑M perpetuals) and **OKX Perpetuals**.
- Strategy: **cross‑venue delta‑neutral arbitrage**, opening **simultaneous opposite positions** to capture price dislocations.
- Costs: include **taker/maker fees**, **funding rate impact** (next window), **slippage**, **latency risk**, **withdrawal/transfer ignored** (no transfers between venues for v1).
- Positioning: support **hedge/one‑way position modes** per venue; allow configuring **leverage** and **margin type** (isolated/cross, default isolated).
- Risk: enforce **per‑venue notional caps**, **per‑symbol caps**, **cross‑venue net exposure ≈ 0**, **kill‑switch**, **cancel‑on‑disconnect**.
- Execution: non‑atomic across venues; implement **two‑phase commit** with **rescue/hedge‑out** if only one leg fills.
- Profiles: `paper`, `testnet`, `live` — **same code**, switch by config/ENV.
- SAFE_MODE: `true` by default → **dry‑run** and `/order/test` routes; flipping to live requires **approvals (Two‑Man Rule)** and **preflight checks**.

---

## B) Config & Secrets (extend)

### B1. `.env` additions (not committed)
```
BINANCE_UM_API_KEY_TESTNET=
BINANCE_UM_API_SECRET_TESTNET=
BINANCE_UM_API_KEY_LIVE=
BINANCE_UM_API_SECRET_LIVE=

OKX_API_KEY_TESTNET=
OKX_API_SECRET_TESTNET=
OKX_API_PASSPHRASE_TESTNET=

OKX_API_KEY_LIVE=
OKX_API_SECRET_LIVE=
OKX_API_PASSPHRASE_LIVE=

EXCHANGE_PROFILE=paper      # paper|testnet|live
SAFE_MODE=true
ALLOW_LIVE_ORDERS=0         # CI guard; live tests only when 1
```

### B2. `configs/config.testnet.yaml` (extend with derivatives)
```yaml
derivatives:
  venues:
    - id: binance_um
      symbols: ["BTCUSDT","ETHUSDT"]
      leverage: 10
      margin_type: isolated
      position_mode: hedge        # or one_way
      routing:
        rest: "<BINANCE_FUTURES_TESTNET_REST>"
        ws:   "<BINANCE_FUTURES_TESTNET_WS>"
    - id: okx_perp
      symbols: ["BTC-USDT-SWAP","ETH-USDT-SWAP"]
      leverage: 10
      margin_type: isolated
      position_mode: hedge
      routing:
        rest: "<OKX_TESTNET_REST>"
        ws:   "<OKX_TESTNET_WS>"
  arbitrage:
    pairs:
      - long: { venue: binance_um, symbol: "BTCUSDT" }
        short:{ venue: okx_perp,  symbol: "BTC-USDT-SWAP" }
      - long: { venue: okx_perp,  symbol: "ETH-USDT-SWAP" }
        short:{ venue: binance_um, symbol: "ETHUSDT" }
    min_edge_bps: 6            # net after fees/funding/slippage
    max_latency_ms: 250
    max_leg_slippage_bps: 3
    prefer_maker: false        # taker/taker default for reliability
    post_only_maker: true
    partial_fill_policy: reject_if_unhedged   # or hedge_remaining
  fees:
    source: "auto"             # auto fetch or "manual"
    manual:
      binance_um: { maker_bps: 1.8, taker_bps: 3.6 }
      okx_perp:   { maker_bps: 2.0, taker_bps: 5.0 }
  funding:
    include_next_window: true
    avoid_window_minutes: 5    # pause opens N minutes before funding
risk:
  notional_caps:
    per_symbol_usd: 2000
    per_venue_usd:  5000
    total_usd:      8000
  cross_venue_delta_abs_max_usd: 200
guards:
  cancel_on_disconnect: true
  clock_skew_guard_ms: 150
  snapshot_diff_check: true
  runaway_breaker:
    place_per_min: 120
    cancel_per_min: 240
```

### B3. `configs/config.live.yaml` — same structure with tighter thresholds and production endpoints.

> NOTE: Codex must consult official docs for **base URLs**, **position modes**, **leverage/margin endpoints**, **precision/filters**; do not hard‑code if discovery exists.

---

## C) New Server Endpoints

- `GET  /api/deriv/status` — venue connectivity, position mode, leverage, margin type per venue/symbol.
- `POST /api/deriv/setup`  — idempotent: set **position mode**, **margin type**, **leverage** per venue/symbol (dry‑run in SAFE_MODE).
- `GET  /api/arb/edge`     — compute live edges for configured pairs; payload returns mid prices, fees, funding impact, net edge bps, tradable size.
- `POST /api/arb/preview`  — for a pair & size, return projected PnL after all costs and rescue plan if one leg fails.
- `POST /api/arb/execute`  — state machine: preflight → place leg A (IOC) → place leg B (IOC) → verify hedge → journal.  
  - SAFE_MODE: no live orders; simulate fills using latest quotes and return dry‑run plan.  
  - On failure of leg B: **hedge‑out** leg A ASAP (market) then mark WARN/ERROR; produce incident record.
- `POST /api/hedge/flatten` — close all open positions per venue/symbol (reduceOnly), restore delta≈0.
- `GET  /api/deriv/positions` — list per venue/symbol positions & margin.

All endpoints must integrate with **System Status** and emit **journal/outbox** entries.

---

## D) Adapters & Market Data

Create:
```
app/exchanges/binance_um.py
app/exchanges/okx_perp.py
app/exchanges/__init__.py
```
Common adapter interface:
```python
class DerivClient:
    def server_time(self): ...
    def ping(self): ...
    def get_filters(self, symbol): ...      # price/qty step, minNotional
    def get_fees(self, symbol): ...         # maker/taker bps
    def get_mark_price(self, symbol): ...
    def get_orderbook_top(self, symbol): ...# best bid/ask + ts
    def get_funding_info(self, symbol): ... # rate, next funding ts
    def set_position_mode(self, mode): ...
    def set_margin_type(self, symbol, margin_type): ...
    def set_leverage(self, symbol, leverage): ...
    def place_order(self, **kwargs): ...    # supports reduceOnly, positionSide
    def cancel_order(self, **kwargs): ...
    def open_orders(self, symbol=None): ...
    def positions(self): ...
```
- REST signing per venue; WS minimal streams for **top of book** and **mark price**; staleness detection.
- Map venue‑specific params (`positionSide`, `reduceOnly`, `tdMode`, etc.) to unified model.
- Respect **rate limits** and surface hits in Status (Rate‑Limit Governor).

---

## E) Arbitrage Engine (state machine)

States: `IDLE -> PREFLIGHT -> LEG_A -> LEG_B -> HEDGED -> DONE` and rescue paths `LEG_A_FILLED_LEG_B_FAIL -> HEDGE_OUT_A`.

PREFLIGHT checks:
1) Both venues connected; `clock_skew` within threshold.
2) Position mode/margin/leverage set & verified.
3) Risk headroom ok (notional caps, delta limits).
4) Edge ≥ `min_edge_bps` after **fees + predicted funding + max_slippage**.
5) Not within `funding.avoid_window_minutes`.
6) Filters/precision allow requested size (rounding ok).

Execution policy (default Taker/Taker IOC):
- Compute **tradable size** limited by both venues’ filters and caps.
- Place **leg A** IOC → if partial or no fill and `partial_fill_policy=reject_if_unhedged` ⇒ cancel & abort.
- If A filled ⇒ place **leg B** IOC; if fails/partial ⇒ immediately **hedge‑out A** with reduceOnly market. Journal incident.
- On success ⇒ mark **HEDGED** with two order ids.

Option Maker/Taker (if `prefer_maker=true`):
- Try maker POST_ONLY on the better venue; if maker not posted (would immediately execute), fallback to IOC Taker plan.

All transitions update **System Status**, with metrics: success rate, average edge realized, p95 cycle time, rescue count.

---

## F) Risk Management (derivatives)

- Enforce **reduceOnly** on hedging/flatten ops.
- Track **maintenance margin** and require headroom buffer; if margin stress detected ⇒ HOLD and disallow new opens.
- **Cross‑venue delta** must remain within `cross_venue_delta_abs_max_usd` (convert using mark price).
- **Runaway breaker**: if >N rescues per minute or fill mismatch ratio grows ⇒ auto HOLD and require manual reset.

---

## G) Tests

Unit (mocks for both venues):
- Filters/rounding tests for symbol constraints.
- Preflight logic under various failures (latency, funding window, no permissions, missing hedge mode).
- State machine tests: success path; leg B failure ⇒ hedge out; maker fallback; rescue counters.
- Risk caps and delta limits.

Integration (conditional on env keys or demo accounts; mark as `skip` if absent):
- `/api/deriv/setup` sets leverage/margin/position mode and verifies via `positions()`/account endpoints.
- `/api/arb/edge` produces sensible values using live orderbooks (testnet/demo).
- SAFE_MODE=true: `/api/arb/execute` returns detailed dry‑run plan; journal writes.
- Optional live smoke when `ALLOW_LIVE_ORDERS=1` and SAFE_MODE=false: open minimal hedged trade, then `/api/hedge/flatten` closes both legs.

Merge‑safety & CI — reuse tests from base ToR; ensure green without secrets; secret‑based jobs optional.

---

## H) CI & Resume Strategy (limits/quota friendly)

- **Chunked commits** with checkpoints:
  - `CHK-1 adapters skeleton`, `CHK-2 status wiring`, `CHK-3 preflight`, `CHK-4 engine`, `CHK-5 tests`, `CHK-6 docs`, `CHK-7 CI`.
- Maintain `RESUME_STATE.json` in repo root, e.g. `{ "checkpoint": 3 }`.  
  - Codex: after hitting limits, **stop at a green checkpoint** with passing local tests; on resume, read `RESUME_STATE.json` and continue.
- GitHub Actions always runnable on partials (unit tests only first, then integration mocked), so every checkpoint is merge‑safe (but keep in one PR as requested).

Example `RESUME_STATE.json`:
```json
{ "checkpoint": 0, "notes": "start" }
```

---

## I) Docs (operator)

- `docs/ARBITRAGE_README.md` — overview, configs, how to inspect edges, how to execute a trade in SAFE_MODE vs live.
- `docs/RISK_AND_GUARDS.md` — limits, HOLD states, how to clear, incident playbook for rescue events.
- `docs/DERIV_SETUP_GUIDE.md` — leverage/margin/position‑mode per venue; demo/testnet guidance.

---

## J) Definition of Done (arbitrage)

- Adapters for **Binance UM** and **OKX Perps** implemented with unified interface.
- `/api/deriv/*`, `/api/arb/*`, `/api/hedge/*` present and functional (paper/testnet).
- Arbitrage engine executes hedged long/short with **rescue** logic; metrics visible in Status.
- Risk caps, delta limits, cancel‑on‑disconnect effective; funding window avoidance works.
- Tests green (unit + mocked integration); CI green; PR mergeable.
- Live path available behind approvals + SAFE_MODE off; optional live smoke when env allows.

---

**End — DERIVATIVES ARBITRAGE ADDENDUM**
