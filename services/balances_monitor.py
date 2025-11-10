"""Monitor and evaluate venue balances to guard against liquidity shortfalls."""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

from app.services import runtime
from exchanges import BinanceFuturesClient, OKXFuturesClient

LOGGER = logging.getLogger(__name__)

_CLIENTS: Dict[str, Any] = {
    "binance": BinanceFuturesClient(),
    "okx": OKXFuturesClient(),
}

_MARGIN_RATIO_THRESHOLD = 0.8
_FREE_RATIO_THRESHOLD = 0.05


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _maybe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_from_mapping(payload: Mapping[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(payload, Mapping):
        return None
    for key in keys:
        if key in payload:
            value = payload.get(key)
            result = _maybe_float(value)
            if result is not None:
                return result
    return None


def _extract_margin_ratio(payload: Mapping[str, Any]) -> float | None:
    candidates = ("margin_ratio", "marginRatio", "mgnRatio", "mgn_ratio", "riskLevel")
    value = _extract_from_mapping(payload, *candidates)
    if value is not None:
        return value
    raw = payload.get("raw")
    if isinstance(raw, Mapping):
        value = _extract_from_mapping(raw, *candidates)
        if value is not None:
            return value
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, Mapping):
                value = _extract_from_mapping(entry, *candidates)
                if value is not None:
                    return value
    return None


def _extract_leverage(payload: Mapping[str, Any]) -> tuple[float | None, float | None]:
    current_candidates = ("leverage", "current_leverage", "currLeverage", "lever")
    max_candidates = ("max_leverage", "maxLeverage", "maxLev")
    current = _extract_from_mapping(payload, *current_candidates)
    max_value = _extract_from_mapping(payload, *max_candidates)
    raw = payload.get("raw")
    if current is None or max_value is None:
        if isinstance(raw, Mapping):
            if current is None:
                current = _extract_from_mapping(raw, *current_candidates)
            if max_value is None:
                max_value = _extract_from_mapping(raw, *max_candidates)
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, Mapping):
                    continue
                if current is None:
                    current = _extract_from_mapping(entry, *current_candidates)
                if max_value is None:
                    max_value = _extract_from_mapping(entry, *max_candidates)
    return current, max_value


def _min_hedge_size() -> float:
    control = runtime.control_as_dict()
    value = control.get("order_notional_usdt")
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _mock_snapshot() -> Dict[str, Dict[str, Any]]:
    return {
        venue: {
            "free_usdt": 1_000_000.0,
            "used_usdt": 0.0,
            "risk_ok": True,
            "reason": "dry_run_mode",
        }
        for venue in _CLIENTS.keys()
    }


def _analyse_limits(venue: str, limits: Mapping[str, Any], min_hedge: float) -> Dict[str, Any]:
    available = _float(
        limits.get("available_balance") or limits.get("free") or limits.get("available")
    )
    total_keys = ("total_balance", "total_equity", "equity", "balance")
    total = 0.0
    for key in total_keys:
        candidate = limits.get(key)
        if candidate is not None:
            total = _float(candidate)
            if total:
                break
    if not total:
        total = available
    used = max(total - available, 0.0)
    reasons: list[str] = []
    risk_ok = True
    if available < max(min_hedge, 0.0):
        risk_ok = False
        reasons.append("free balance below hedge size")
    margin_ratio = _extract_margin_ratio(limits)
    if margin_ratio is not None and margin_ratio >= _MARGIN_RATIO_THRESHOLD:
        risk_ok = False
        reasons.append("margin ratio elevated")
    else:
        if total > 0:
            free_ratio = available / total
            if free_ratio < _FREE_RATIO_THRESHOLD:
                risk_ok = False
                reasons.append("free balance fraction low")
    current_leverage, max_leverage = _extract_leverage(limits)
    if max_leverage and current_leverage:
        try:
            if current_leverage >= max_leverage:
                risk_ok = False
                reasons.append("max leverage reached")
        except TypeError as exc:  # pragma: no cover - defensive
            LOGGER.debug(
                "balance monitor leverage comparison failed",
                extra={"venue": venue},
                exc_info=exc,
            )
    if not reasons:
        reasons.append("ok")
    reason = "; ".join(reasons)
    return {
        "venue": venue,
        "free_usdt": round(available, 6),
        "used_usdt": round(max(used, 0.0), 6),
        "risk_ok": risk_ok,
        "reason": reason,
    }


def evaluate_balances(*, auto_hold: bool = True) -> Dict[str, Any]:
    """Collect balance snapshots and update runtime liquidity safety state."""

    min_hedge = _min_hedge_size()
    dry_run_mode = runtime.is_dry_run_mode()

    if dry_run_mode:
        snapshot = _mock_snapshot()
        runtime.update_liquidity_snapshot(
            snapshot,
            blocked=False,
            reason="dry_run_mode",
            source="balances_monitor",
            auto_hold=False,
        )
        return {
            "per_venue": snapshot,
            "liquidity_blocked": False,
            "reason": "dry_run_mode",
        }

    snapshot: Dict[str, Dict[str, Any]] = {}
    blocked = False
    aggregate_reason = "ok"

    for venue, client in _CLIENTS.items():
        try:
            limits = client.get_account_limits()
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.exception("Failed to fetch account limits", extra={"venue": venue})
            snapshot[venue] = {
                "venue": venue,
                "free_usdt": 0.0,
                "used_usdt": 0.0,
                "risk_ok": False,
                "reason": f"balance unavailable: {exc}",
            }
            if not blocked:
                aggregate_reason = f"{venue}:balance_unavailable"
            blocked = True
            continue
        entry = _analyse_limits(venue, limits or {}, min_hedge)
        snapshot[venue] = entry
        if not entry.get("risk_ok", False):
            if not blocked:
                aggregate_reason = f"{venue}:{entry.get('reason', 'liquidity_blocked')}"
            blocked = True

    runtime.update_liquidity_snapshot(
        snapshot,
        blocked=blocked,
        reason=aggregate_reason,
        source="balances_monitor",
        auto_hold=auto_hold,
    )
    return {
        "per_venue": snapshot,
        "liquidity_blocked": blocked,
        "reason": aggregate_reason,
    }


__all__ = ["evaluate_balances"]
