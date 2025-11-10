from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Iterable, List, Mapping

from .. import ledger
from ..util.venues import VENUE_ALIASES
from . import risk_alerts
from .portfolio import PortfolioPosition, PortfolioSnapshot
from .pnl import Fill, compute_realized_pnl
from .runtime import (
    RiskBreach,
    RiskState,
    get_reconciliation_status,
    get_state,
)
from services import reconciler


LOGGER = logging.getLogger(__name__)


if TYPE_CHECKING:  # pragma: no cover
    from .arbitrage import Plan


@dataclass(frozen=True)
class RiskMetrics:
    positions_usdt: Dict[str, float]
    open_orders: Dict[str, int]
    daily_realized_usdt: float


def _normalise_symbol(value: str) -> str:
    return str(value or "").upper()


def _normalise_venue(value: str) -> str:
    return str(value or "").lower()


def _start_of_day() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def _positions_from_snapshot(snapshot: PortfolioSnapshot | None) -> Dict[str, float]:
    if snapshot is None:
        rows = ledger.fetch_positions()
        return _positions_from_rows(rows)
    totals: Dict[str, float] = {}
    positions: Iterable[PortfolioPosition] = getattr(snapshot, "positions", [])
    for position in positions:
        symbol = _normalise_symbol(position.symbol)
        totals[symbol] = totals.get(symbol, 0.0) + float(position.notional)
    return totals


def _positions_from_rows(rows: Iterable[Mapping[str, object]]) -> Dict[str, float]:
    totals: Dict[str, float] = {}
    for row in rows:
        symbol = _normalise_symbol(row.get("symbol"))
        qty = abs(float(row.get("base_qty", 0.0)))
        price = float(row.get("avg_price", 0.0))
        totals[symbol] = totals.get(symbol, 0.0) + qty * price
    return totals


def _open_orders_from_payload(open_orders: Iterable[Mapping[str, object]] | None) -> Dict[str, int]:
    if open_orders is None:
        open_orders = ledger.fetch_open_orders()
    counts: Dict[str, int] = {}
    for entry in open_orders:
        venue = _normalise_venue(entry.get("venue"))
        counts[venue] = counts.get(venue, 0) + 1
    return counts


def _daily_realized_loss() -> float:
    since = _start_of_day()
    fills = [Fill.from_mapping(row) for row in ledger.fetch_fills_since(since)]
    return compute_realized_pnl(fills)


def _resolve_limit(mapping: Dict[str, float], scope: str) -> float | None:
    if not mapping:
        return None
    scope_key = scope
    if scope_key in mapping:
        return mapping[scope_key]
    return mapping.get("__default__")


def _active_breaches(state: RiskState, metrics: RiskMetrics) -> list[RiskBreach]:
    breaches: list[RiskBreach] = []
    position_limits = state.limits.max_position_usdt
    if "__default__" in position_limits:
        default_limit = position_limits["__default__"]
        if default_limit > 0:
            for symbol, value in metrics.positions_usdt.items():
                if value > default_limit:
                    breaches.append(
                        RiskBreach(
                            limit="max_position_usdt",
                            scope=symbol,
                            current=value,
                            threshold=default_limit,
                            detail="position limit exceeded",
                        )
                    )
    for symbol, threshold in position_limits.items():
        if symbol == "__default__":
            continue
        if threshold <= 0:
            continue
        current = metrics.positions_usdt.get(symbol, 0.0)
        if current > threshold:
            breaches.append(
                RiskBreach(
                    limit="max_position_usdt",
                    scope=symbol,
                    current=current,
                    threshold=threshold,
                    detail="position limit exceeded",
                )
            )

    open_limits = state.limits.max_open_orders
    if "__default__" in open_limits:
        default_orders = int(open_limits["__default__"])
        if default_orders >= 0:
            for venue, count in metrics.open_orders.items():
                if count > default_orders:
                    breaches.append(
                        RiskBreach(
                            limit="max_open_orders",
                            scope=venue,
                            current=float(count),
                            threshold=float(default_orders),
                            detail="open orders limit exceeded",
                        )
                    )
    for venue, threshold in open_limits.items():
        if venue == "__default__":
            continue
        if threshold < 0:
            continue
        count = metrics.open_orders.get(venue, 0)
        if count > int(threshold):
            breaches.append(
                RiskBreach(
                    limit="max_open_orders",
                    scope=venue,
                    current=float(count),
                    threshold=float(threshold),
                    detail="open orders limit exceeded",
                )
            )

    loss_limit = state.limits.max_daily_loss_usdt
    if loss_limit is not None and loss_limit > 0:
        if metrics.daily_realized_usdt < -loss_limit:
            breaches.append(
                RiskBreach(
                    limit="max_daily_loss_usdt",
                    scope="daily",
                    current=metrics.daily_realized_usdt,
                    threshold=loss_limit,
                    detail="daily loss limit exceeded",
                )
            )
    return breaches


def refresh_runtime_state(
    *,
    snapshot: PortfolioSnapshot | None = None,
    open_orders: Iterable[Mapping[str, object]] | None = None,
) -> RiskState:
    state = get_state()
    positions = _positions_from_snapshot(snapshot)
    open_orders_payload = list(open_orders) if open_orders is not None else None
    orders = _open_orders_from_payload(open_orders_payload)
    daily_loss = _daily_realized_loss()
    metrics = RiskMetrics(positions_usdt=positions, open_orders=orders, daily_realized_usdt=daily_loss)
    state.risk.current.position_usdt = dict(positions)
    state.risk.current.open_orders = dict(orders)
    state.risk.current.daily_loss_usdt = daily_loss
    state.risk.breaches = _active_breaches(state.risk, metrics)
    risk_alerts.evaluate_alerts()
    try:
        reconciler.reconcile(open_orders=open_orders_payload)
    except Exception as exc:
        LOGGER.warning("risk reconciler failed: %s", exc)
    return state.risk


def _plan_order_counts(plan: "Plan") -> Counter[str]:
    counts: Counter[str] = Counter()
    for leg in plan.legs:
        venue = VENUE_ALIASES.get(str(getattr(leg, "exchange", "")).lower(), str(getattr(leg, "exchange", "")).lower())
        counts[_normalise_venue(venue)] += 1
    return counts


def _plan_limit_reasons(plan: "Plan", risk_state: RiskState) -> list[str]:
    reasons: list[str] = []
    symbol = _normalise_symbol(plan.symbol)
    current_position = risk_state.current.position_usdt.get(symbol, 0.0)
    limit = _resolve_limit(risk_state.limits.max_position_usdt, symbol)
    if limit is not None and limit > 0:
        projected = current_position + max(plan.notional, 0.0)
        if projected > limit:
            reasons.append(
                f"risk:max_position_usdt {symbol} limit {limit:.2f} (projected {projected:.2f})"
            )

    planned_orders = _plan_order_counts(plan)
    for venue, additional in planned_orders.items():
        limit_value = _resolve_limit(risk_state.limits.max_open_orders, venue)
        if limit_value is None:
            continue
        threshold = int(limit_value)
        if threshold < 0:
            continue
        current_orders = risk_state.current.open_orders.get(venue, 0)
        if current_orders + additional > threshold:
            reasons.append(
                f"risk:max_open_orders {venue} limit {threshold} (projected {current_orders + additional})"
            )

    loss_limit = risk_state.limits.max_daily_loss_usdt
    if loss_limit is not None and loss_limit > 0:
        if risk_state.current.daily_loss_usdt < -loss_limit:
            reasons.append(
                f"risk:max_daily_loss_usdt limit {loss_limit:.2f} breached ({risk_state.current.daily_loss_usdt:.2f})"
            )

    return reasons


def guard_plan(plan: "Plan") -> tuple[bool, str | None, RiskState]:
    risk_state = refresh_runtime_state()
    reconciliation = get_reconciliation_status()
    if bool(reconciliation.get("desync_detected")):
        reason = "desync"
        ledger.record_event(
            level="WARNING",
            code="risk_blocked",
            payload={"symbol": getattr(plan, "symbol", "unknown"), "reasons": [reason]},
        )
        return False, reason, risk_state
    reasons = _plan_limit_reasons(plan, risk_state)
    if reasons:
        ledger.record_event(
            level="WARNING",
            code="risk_blocked",
            payload={"symbol": plan.symbol, "reasons": reasons},
        )
        return False, "; ".join(reasons), risk_state
    return True, None, risk_state


def evaluate_plan(plan: "Plan", *, risk_state: RiskState | None = None) -> None:
    state = risk_state or refresh_runtime_state()
    reasons = _plan_limit_reasons(plan, state)
    if reasons:
        plan.viable = False
        if plan.reason:
            reasons.insert(0, plan.reason)
        plan.reason = "; ".join(reasons)


def risk_overview(snapshot: PortfolioSnapshot | None = None) -> Dict[str, object]:
    state = refresh_runtime_state(snapshot=snapshot)
    positions_rows = ledger.fetch_positions()
    exposures: List[Dict[str, object]] = []
    exposure_totals: Dict[str, Dict[str, float]] = {}
    for row in positions_rows:
        venue = _normalise_venue(row.get("venue"))
        symbol = _normalise_symbol(row.get("symbol"))
        qty = float(row.get("base_qty", 0.0))
        price = float(row.get("avg_price", 0.0))
        notional = abs(qty) * price
        exposures.append(
            {
                "venue": venue,
                "symbol": symbol,
                "qty": qty,
                "avg_price": price,
                "notional": notional,
            }
        )
        venue_totals = exposure_totals.setdefault(venue, {})
        venue_totals[symbol] = venue_totals.get(symbol, 0.0) + notional
    limits_hit = sorted({breach.limit for breach in state.breaches})
    return {
        "limits": state.limits.as_dict(),
        "current": state.current.as_dict(),
        "breaches": [breach.as_dict() for breach in state.breaches],
        "positions_usdt": dict(state.current.position_usdt),
        "open_orders": dict(state.current.open_orders),
        "daily_loss_usdt": state.current.daily_loss_usdt,
        "exposures": exposures,
        "exposure_totals": exposure_totals,
        "limits_hit": limits_hit,
    }
