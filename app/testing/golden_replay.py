"""Deterministic golden replay harness used by regression tests.

The implementation intentionally avoids any external side effects and works
entirely in-memory using the static payload that is shipped with the test
suite.  Scenarios describe an initial runtime snapshot, a sequence of events
and a set of expectations/invariants that must be satisfied after processing
all events.

The harness deliberately keeps the execution rules simple but enforces the key
invariants that matter for regression detection:

* no trading is carried out while HOLD or SAFE mode is active;
* daily loss limits and notional caps are honoured;
* realised PnL is tracked using ``Decimal`` arithmetic to avoid drift;
* watchdog style recovery hooks can be asserted in tests.

Scenarios can be expressed in JSON or YAML (if ``pyyaml`` is installed).  The
structure of a scenario is intentionally lightweight to keep the test runtime
fast and deterministic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence

try:  # Optional dependency; YAML is not required for the JSON scenarios.
    import yaml  # type: ignore
except Exception:  # pragma: no cover - defensive, YAML support is optional.
    yaml = None

getcontext().prec = 18


def _to_decimal(value: Any) -> Decimal:
    """Best-effort coercion to :class:`Decimal`.

    Numbers inside the scenarios are frequently encoded as strings to avoid any
    JSON floating point ambiguity.  ``Decimal`` arithmetic ensures that
    calculations remain deterministic.
    """

    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return Decimal("0")
        try:
            return Decimal(stripped)
        except InvalidOperation as exc:  # pragma: no cover - defensive path.
            raise ValueError(f"invalid decimal value: {value!r}") from exc
    raise TypeError(f"unsupported decimal value: {value!r}")


@dataclass(frozen=True)
class ScenarioLimits:
    daily_loss_limit: Decimal = Decimal("0")
    notional_cap: Decimal = Decimal("0")


@dataclass(frozen=True)
class ScenarioControl:
    hold: bool = False
    safe_mode: bool = False
    auto_trade: bool = True


@dataclass(frozen=True)
class ScenarioPosition:
    symbol: str
    qty: Decimal
    avg_price: Decimal


@dataclass(frozen=True)
class ScenarioInitialState:
    balances: Mapping[str, Decimal]
    positions: Mapping[str, ScenarioPosition]
    control: ScenarioControl
    limits: ScenarioLimits


@dataclass(frozen=True)
class GoldenScenario:
    name: str
    initial_state: ScenarioInitialState
    events: Sequence[Mapping[str, Any]]
    expectations: Mapping[str, Any]


@dataclass
class _RuntimePosition:
    qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")


@dataclass
class _RuntimeContext:
    name: str
    balances: MutableMapping[str, Decimal]
    positions: MutableMapping[str, _RuntimePosition]
    hold: bool
    safe_mode: bool
    auto_trade: bool
    daily_loss_limit: Decimal
    notional_cap: Decimal
    realized_pnl: Decimal = Decimal("0")
    daily_loss_breached: bool = False
    market_data_ok: bool = True
    watchdog_recoveries: int = 0
    violations: List[Dict[str, Any]] = field(default_factory=list)
    trade_log: List[Dict[str, Any]] = field(default_factory=list)
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    mark_prices: MutableMapping[str, Decimal] = field(default_factory=dict)


def _load_payload(path: Path) -> Mapping[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise RuntimeError("pyyaml is required to load YAML scenarios")
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    else:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    if not isinstance(loaded, Mapping):
        raise ValueError(f"scenario root must be a mapping: {path}")
    return loaded


def _parse_positions(payload: Iterable[Mapping[str, Any]]) -> Dict[str, ScenarioPosition]:
    positions: Dict[str, ScenarioPosition] = {}
    for entry in payload:
        symbol = str(entry.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("scenario position missing symbol")
        qty = _to_decimal(entry.get("qty", "0"))
        avg_price = _to_decimal(entry.get("avg_price", "0"))
        positions[symbol] = ScenarioPosition(symbol=symbol, qty=qty, avg_price=avg_price)
    return positions


def _parse_initial_state(payload: Mapping[str, Any]) -> ScenarioInitialState:
    balances_payload = payload.get("balances") or {}
    if not isinstance(balances_payload, Mapping):
        raise ValueError("initial_state.balances must be a mapping")
    balances = {str(symbol): _to_decimal(amount) for symbol, amount in balances_payload.items()}

    positions_payload = payload.get("positions") or []
    if not isinstance(positions_payload, Iterable):
        raise ValueError("initial_state.positions must be a list")
    positions = _parse_positions(pos for pos in positions_payload if isinstance(pos, Mapping))

    control_payload = payload.get("control") or {}
    control = ScenarioControl(
        hold=bool(control_payload.get("hold", False)),
        safe_mode=bool(control_payload.get("safe_mode", False)),
        auto_trade=bool(control_payload.get("auto_trade", True)),
    )

    limits_payload = payload.get("limits") or {}
    if not isinstance(limits_payload, Mapping):
        raise ValueError("initial_state.limits must be a mapping")
    limits = ScenarioLimits(
        daily_loss_limit=_to_decimal(limits_payload.get("daily_loss_limit", "0")),
        notional_cap=_to_decimal(limits_payload.get("notional_cap", "0")),
    )

    return ScenarioInitialState(
        balances=balances, positions=positions, control=control, limits=limits
    )


def _normalise_events(events: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    normalised: List[Mapping[str, Any]] = []
    for entry in events:
        if not isinstance(entry, Mapping):
            raise ValueError("scenario event must be a mapping")
        if "type" not in entry:
            raise ValueError("scenario event missing type")
        normalised.append(dict(entry))
    return normalised


def _normalise_expectations(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not payload:
        return {}
    if not isinstance(payload, Mapping):
        raise ValueError("expectations must be a mapping")
    return dict(payload)


def load_scenario(path: Path) -> GoldenScenario:
    """Load a golden scenario definition from ``path``."""

    payload = _load_payload(path)
    name = str(payload.get("name") or path.stem)
    initial_state_payload = payload.get("initial_state") or {}
    if not isinstance(initial_state_payload, Mapping):
        raise ValueError("scenario missing initial_state")
    initial_state = _parse_initial_state(initial_state_payload)
    events_payload = payload.get("events") or []
    if not isinstance(events_payload, Sequence):
        raise ValueError("scenario events must be a list")
    events = _normalise_events(event for event in events_payload if isinstance(event, Mapping))
    expectations = _normalise_expectations(payload.get("expectations"))
    return GoldenScenario(
        name=name, initial_state=initial_state, events=events, expectations=expectations
    )


def _ensure_position(ctx: _RuntimeContext, symbol: str) -> _RuntimePosition:
    if symbol not in ctx.positions:
        ctx.positions[symbol] = _RuntimePosition()
    return ctx.positions[symbol]


def _record_violation(ctx: _RuntimeContext, *, kind: str, details: Mapping[str, Any]) -> None:
    ctx.violations.append({"type": kind, "details": dict(details)})


def _handle_trade(ctx: _RuntimeContext, event: Mapping[str, Any]) -> None:
    symbol = str(event.get("symbol") or "").strip()
    side = str(event.get("side") or "").lower()
    qty = _to_decimal(event.get("qty", "0"))
    price = _to_decimal(event.get("price", "0"))
    if not symbol or qty <= 0 or price <= 0:
        raise ValueError(f"invalid trade event: {event}")

    trade_context = {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "during_hold": ctx.hold,
        "during_safe_mode": ctx.safe_mode,
    }

    if ctx.hold or ctx.safe_mode or not ctx.auto_trade:
        _record_violation(ctx, kind="trade_blocked", details=trade_context)
        return

    notional = qty * price
    if ctx.notional_cap and notional > ctx.notional_cap:
        _record_violation(
            ctx, kind="notional_cap_exceeded", details={**trade_context, "notional": notional}
        )
        return

    position = _ensure_position(ctx, symbol)

    if side == "buy":
        if position.qty <= 0:
            position.avg_price = price
            position.qty = qty
        else:
            total_qty = position.qty + qty
            if total_qty > 0:
                position.avg_price = (position.avg_price * position.qty + price * qty) / total_qty
            position.qty = total_qty
    elif side == "sell":
        if position.qty <= 0:
            _record_violation(ctx, kind="unexpected_short", details=trade_context)
            return
        execute_qty = min(qty, position.qty)
        pnl_delta = (price - position.avg_price) * execute_qty
        ctx.realized_pnl += pnl_delta
        position.qty -= execute_qty
        if position.qty == 0:
            position.avg_price = Decimal("0")
    else:  # pragma: no cover - scenario authoring error
        raise ValueError(f"unsupported trade side: {side}")

    ctx.trade_log.append({**trade_context, "executed": True, "notional": notional})

    if ctx.daily_loss_limit > 0 and ctx.realized_pnl < -ctx.daily_loss_limit:
        ctx.hold = True
        ctx.auto_trade = False
        ctx.daily_loss_breached = True


def _handle_set_hold(ctx: _RuntimeContext, event: Mapping[str, Any]) -> None:
    ctx.hold = bool(event.get("active", True))
    if ctx.hold:
        ctx.auto_trade = False


def _handle_set_safe_mode(ctx: _RuntimeContext, event: Mapping[str, Any]) -> None:
    ctx.safe_mode = bool(event.get("active", True))
    if ctx.safe_mode:
        ctx.auto_trade = False
    else:
        ctx.auto_trade = bool(event.get("auto_trade", ctx.auto_trade))


def _handle_market(ctx: _RuntimeContext, event: Mapping[str, Any]) -> None:
    symbol = str(event.get("symbol") or "").strip()
    price = _to_decimal(event.get("price", "0"))
    if symbol and price > 0:
        ctx.mark_prices[symbol] = price


def _handle_ws_glitch(ctx: _RuntimeContext, event: Mapping[str, Any]) -> None:
    ctx.market_data_ok = False
    ctx.safe_mode = True
    ctx.auto_trade = False


def _handle_ws_recover(ctx: _RuntimeContext, event: Mapping[str, Any]) -> None:
    ctx.market_data_ok = True
    ctx.safe_mode = bool(event.get("safe_mode", False))
    ctx.auto_trade = not ctx.safe_mode
    ctx.watchdog_recoveries += 1


_EVENT_HANDLERS = {
    "trade": _handle_trade,
    "market": _handle_market,
    "set_hold": _handle_set_hold,
    "set_safe_mode": _handle_set_safe_mode,
    "ws_glitch": _handle_ws_glitch,
    "ws_recover": _handle_ws_recover,
}


def _apply_event(ctx: _RuntimeContext, event: Mapping[str, Any]) -> None:
    event_type = str(event.get("type") or "").lower()
    handler = _EVENT_HANDLERS.get(event_type)
    if handler is None:
        raise ValueError(f"unsupported scenario event type: {event_type}")
    before_hold = ctx.hold
    before_safe = ctx.safe_mode
    handler(ctx, event)

    ctx.timeline.append(
        {
            "event": dict(event),
            "hold": ctx.hold,
            "safe_mode": ctx.safe_mode,
            "auto_trade": ctx.auto_trade,
            "realized_pnl": ctx.realized_pnl,
            "daily_loss_breached": ctx.daily_loss_breached,
            "market_data_ok": ctx.market_data_ok,
            "hold_before": before_hold,
            "safe_mode_before": before_safe,
        }
    )


def run_scenario(scenario: GoldenScenario) -> Dict[str, Any]:
    """Execute ``scenario`` and return the resulting runtime snapshot."""

    runtime_positions: Dict[str, _RuntimePosition] = {
        symbol: _RuntimePosition(qty=position.qty, avg_price=position.avg_price)
        for symbol, position in scenario.initial_state.positions.items()
    }
    runtime_balances: Dict[str, Decimal] = dict(scenario.initial_state.balances)

    ctx = _RuntimeContext(
        name=scenario.name,
        balances=runtime_balances,
        positions=runtime_positions,
        hold=scenario.initial_state.control.hold,
        safe_mode=scenario.initial_state.control.safe_mode,
        auto_trade=scenario.initial_state.control.auto_trade,
        daily_loss_limit=scenario.initial_state.limits.daily_loss_limit,
        notional_cap=scenario.initial_state.limits.notional_cap,
    )

    for event in scenario.events:
        _apply_event(ctx, event)

    final_positions = {symbol: position.qty for symbol, position in ctx.positions.items()}

    return {
        "name": scenario.name,
        "final_positions": final_positions,
        "hold": ctx.hold,
        "safe_mode": ctx.safe_mode,
        "auto_trade": ctx.auto_trade,
        "realized_pnl": ctx.realized_pnl,
        "daily_loss": {"limit": ctx.daily_loss_limit, "breached": ctx.daily_loss_breached},
        "notional_cap": ctx.notional_cap,
        "violations": ctx.violations,
        "trade_log": ctx.trade_log,
        "timeline": ctx.timeline,
        "watchdog": {"market_data_ok": ctx.market_data_ok, "recoveries": ctx.watchdog_recoveries},
    }


def assert_invariants(result: Mapping[str, Any]) -> None:
    """Validate core invariants after running a golden scenario."""

    violations = list(result.get("violations", []))
    if violations:
        raise AssertionError(f"scenario invariant violations detected: {violations}")

    realized_pnl = result.get("realized_pnl")
    if not isinstance(realized_pnl, Decimal):
        raise AssertionError("realized_pnl must be a Decimal")

    timeline = result.get("timeline") or []
    for snapshot in timeline:
        if not isinstance(snapshot, Mapping):
            continue
        event_type = snapshot.get("event", {}).get("type")
        if snapshot.get("hold_before") and event_type == "trade":
            raise AssertionError("trades executed while hold was active")
        if snapshot.get("safe_mode_before") and event_type == "trade":
            raise AssertionError("trades executed while safe_mode was active")

    daily_loss = result.get("daily_loss", {})
    if isinstance(daily_loss, Mapping):
        limit = daily_loss.get("limit")
        breached = bool(daily_loss.get("breached"))
        if isinstance(limit, Decimal) and limit > 0 and realized_pnl < -limit and not breached:
            raise AssertionError("daily loss limit exceeded without breach flag")


__all__ = ["GoldenScenario", "load_scenario", "run_scenario", "assert_invariants"]
