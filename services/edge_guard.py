"""Adaptive gatekeeper that pauses new hedges in toxic market conditions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from statistics import mean
from typing import Dict, Iterable, List, Mapping, Tuple

from app.services import risk_guard, runtime
from services import balances_monitor
from pnl_history_store import list_recent as list_recent_pnl_snapshots
from positions import list_open_positions
from .execution_stats_store import list_recent as list_recent_execution_stats


_SLIPPAGE_LOOKBACK = 8
_SLIPPAGE_MIN_SAMPLES = 3
_SLIPPAGE_THRESHOLD_BPS = 5.0
_FAILURE_RATE_LOOKBACK = 8
_FAILURE_RATE_THRESHOLD = 0.4
_PNL_LOOKBACK = 5
_EXPOSURE_LIMIT_FRACTION = 0.7
_MIN_EXPOSURE_ALERT_USD = 50_000.0


@dataclass(frozen=True)
class EdgeGuardContext:
    """Snapshot of guard inputs for debugging or UI consumption."""

    hold_active: bool
    hold_reason: str
    partial_positions: int
    avg_slippage_bps: float | None
    failure_rate: float | None
    pnl_trend_negative: bool
    exposure_total: float
    liquidity_blocked: bool
    liquidity_reason: str


def _normalise_symbol(symbol: str | None) -> str:
    return str(symbol or "").upper()


def _current_positions() -> List[Dict[str, object]]:
    return list_open_positions()


def _recent_execution_stats(limit: int) -> List[Dict[str, object]]:
    return list_recent_execution_stats(limit=limit)


def _recent_pnl_snapshots(limit: int) -> List[Dict[str, object]]:
    return list_recent_pnl_snapshots(limit=limit)


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _max_total_notional_limit() -> float:
    for key in ("MAX_TOTAL_NOTIONAL_USDT", "MAX_TOTAL_NOTIONAL_USD"):
        raw = os.getenv(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return 0.0


def _heavy_exposure(exposure_total: float) -> bool:
    limit = _max_total_notional_limit()
    if limit > 0:
        return exposure_total >= (limit * _EXPOSURE_LIMIT_FRACTION)
    return exposure_total >= _MIN_EXPOSURE_ALERT_USD


def _avg_slippage(symbol: str | None) -> Tuple[float | None, float | None]:
    symbol_filter = _normalise_symbol(symbol)
    records = _recent_execution_stats(_SLIPPAGE_LOOKBACK)
    relevant: List[Dict[str, object]] = []
    for entry in records[::-1]:  # older → newer for stability
        if bool(entry.get("dry_run")):
            continue
        entry_symbol = _normalise_symbol(entry.get("symbol"))
        if symbol_filter and entry_symbol and entry_symbol != symbol_filter:
            continue
        relevant.append(entry)
    if not relevant:
        return None, None
    slippages = [
        abs(_float(entry.get("slippage_bps")))
        for entry in relevant
        if entry.get("slippage_bps") is not None
    ]
    avg_slippage = mean(slippages) if len(slippages) >= _SLIPPAGE_MIN_SAMPLES else None
    total = len(relevant[:_FAILURE_RATE_LOOKBACK])
    failures = sum(1 for entry in relevant[:_FAILURE_RATE_LOOKBACK] if not bool(entry.get("success")))
    failure_rate = (failures / total) if total else None
    return avg_slippage, failure_rate


def _pnl_downtrend_with_exposure() -> Tuple[bool, float]:
    snapshots = _recent_pnl_snapshots(_PNL_LOOKBACK)
    if len(snapshots) < _PNL_LOOKBACK:
        latest_total = _float(snapshots[0].get("total_exposure_usd_total")) if snapshots else 0.0
        return False, latest_total
    chronological = list(reversed(snapshots))
    unrealised = [_float(entry.get("unrealized_pnl_total")) for entry in chronological]
    downtrend = all(unrealised[i] < unrealised[i - 1] for i in range(1, len(unrealised)))
    latest_total = _float(snapshots[0].get("total_exposure_usd_total"))
    if downtrend and _heavy_exposure(latest_total):
        return True, latest_total
    return False, latest_total


def _partial_hedges_open(positions: Iterable[Mapping[str, object]]) -> int:
    count = 0
    for position in positions:
        status = str(position.get("status") or "").lower()
        if status != "partial":
            continue
        if bool(position.get("simulated")):
            continue
        count += 1
    return count


def _build_context(
    *,
    hold_active: bool,
    hold_reason: str,
    partial_positions: int,
    avg_slippage: float | None,
    failure_rate: float | None,
    pnl_trend_negative: bool,
    exposure_total: float,
    liquidity_blocked: bool,
    liquidity_reason: str,
) -> EdgeGuardContext:
    return EdgeGuardContext(
        hold_active=hold_active,
        hold_reason=hold_reason,
        partial_positions=partial_positions,
        avg_slippage_bps=avg_slippage,
        failure_rate=failure_rate,
        pnl_trend_negative=pnl_trend_negative,
        exposure_total=exposure_total,
        liquidity_blocked=liquidity_blocked,
        liquidity_reason=liquidity_reason,
    )


def allowed_to_trade(symbol_pair: str | None = None) -> Tuple[bool, str]:
    """Evaluate whether a new hedge leg should be attempted."""

    safety = runtime.get_safety_status()
    reconciliation = runtime.get_reconciliation_status()
    if bool(reconciliation.get("desync_detected")):
        return False, "desync"
    hold_active = bool(safety.get("hold_active"))
    hold_reason = str(safety.get("hold_reason") or "")
    if hold_active:
        if hold_reason.upper().startswith(risk_guard.AUTO_THROTTLE_PREFIX):
            return False, "risk_throttle_active"
        return False, "hold_active"

    liquidity_status = balances_monitor.evaluate_balances()
    if bool(liquidity_status.get("liquidity_blocked")):
        reason = str(liquidity_status.get("reason") or "liquidity_blocked")
        return False, reason

    positions = _current_positions()
    partial_count = _partial_hedges_open(positions)
    if partial_count:
        return False, "partial_hedge_outstanding"

    avg_slippage, failure_rate = _avg_slippage(symbol_pair)
    if avg_slippage is not None and avg_slippage > _SLIPPAGE_THRESHOLD_BPS:
        return False, "slippage_degraded"
    if failure_rate is not None and failure_rate >= _FAILURE_RATE_THRESHOLD:
        return False, "execution_fail_rate_high"

    pnl_downtrend, exposure_total = _pnl_downtrend_with_exposure()
    if pnl_downtrend:
        return False, "pnl_downtrend_with_exposure"

    return True, "ok"


def current_context(symbol_pair: str | None = None) -> EdgeGuardContext:
    """Return the current guard inputs for observability (dashboard/tests)."""

    safety = runtime.get_safety_status()
    hold_active = bool(safety.get("hold_active"))
    hold_reason = str(safety.get("hold_reason") or "")
    positions = _current_positions()
    partial_count = _partial_hedges_open(positions)
    avg_slippage, failure_rate = _avg_slippage(symbol_pair)
    pnl_downtrend, exposure_total = _pnl_downtrend_with_exposure()
    liquidity_status = runtime.get_liquidity_status()
    liquidity_blocked = bool(liquidity_status.get("liquidity_blocked"))
    liquidity_reason = str(liquidity_status.get("reason") or "ok")
    return _build_context(
        hold_active=hold_active,
        hold_reason=hold_reason,
        partial_positions=partial_count,
        avg_slippage=avg_slippage,
        failure_rate=failure_rate,
        pnl_trend_negative=pnl_downtrend,
        exposure_total=exposure_total,
        liquidity_blocked=liquidity_blocked,
        liquidity_reason=liquidity_reason,
    )


__all__ = ["allowed_to_trade", "current_context", "EdgeGuardContext"]
