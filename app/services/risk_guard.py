"""Automatic risk throttling helpers that enforce HOLD on hard breaches."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from positions_store import list_records as list_position_records

from ..opsbot import notifier
from . import hedge_log, runtime


AUTO_THROTTLE_PREFIX = "AUTO_THROTTLE/"
REASON_RUNAWAY_NOTIONAL = f"{AUTO_THROTTLE_PREFIX}RUNAWAY_TOTAL_NOTIONAL"
REASON_RUNAWAY_POSITIONS = f"{AUTO_THROTTLE_PREFIX}RUNAWAY_OPEN_POSITIONS"
REASON_AUTO_HEDGE_FAILURES = f"{AUTO_THROTTLE_PREFIX}AUTO_HEDGE_FAILURES"
REASON_PARTIAL_STALLED = f"{AUTO_THROTTLE_PREFIX}PARTIAL_HEDGE_STALLED"
REASON_ORDER_REJECTIONS = f"{AUTO_THROTTLE_PREFIX}ORDER_REJECTIONS"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _parse_timestamp(raw: object) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _normalise_reason(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        text = "UNKNOWN"
    prefix = AUTO_THROTTLE_PREFIX
    if not text.upper().startswith(prefix):
        text = f"{prefix}{text}"
    inner = text[len(prefix) :]
    code, sep, tail = inner.partition(":")
    code = code.upper()
    if sep:
        return f"{prefix}{code}{sep}{tail}"
    return f"{prefix}{code}"


def _active_positions(records: Iterable[Mapping[str, object]]) -> List[Mapping[str, object]]:
    active: List[Mapping[str, object]] = []
    for entry in records:
        status = str(entry.get("status") or "").lower()
        if status in {"closed", "simulated"}:
            continue
        if bool(entry.get("simulated")):
            continue
        active.append(entry)
    return active


def _total_notional_usdt(records: Iterable[Mapping[str, object]]) -> float:
    total = 0.0
    for entry in records:
        try:
            total += abs(float(entry.get("notional_usdt") or 0.0))
        except (TypeError, ValueError):
            continue
    return total


def _partial_stalled(
    records: Iterable[Mapping[str, object]],
    *,
    now: datetime,
    threshold_seconds: float,
) -> Optional[Mapping[str, object]]:
    if threshold_seconds <= 0:
        return None
    stalled: Tuple[Optional[datetime], Optional[Mapping[str, object]]] = (None, None)
    for entry in records:
        status = str(entry.get("status") or "").lower()
        if status != "partial":
            continue
        if bool(entry.get("simulated")):
            continue
        opened_at = _parse_timestamp(entry.get("timestamp"))
        if not opened_at:
            continue
        age = (now - opened_at).total_seconds()
        if age < threshold_seconds:
            continue
        legs = entry.get("legs")
        incomplete = False
        if isinstance(legs, list):
            for leg in legs:
                if not isinstance(leg, Mapping):
                    continue
                leg_status = str(leg.get("status") or "").lower()
                if leg_status in {"filled", "closed"}:
                    continue
                incomplete = True
        else:
            incomplete = True
        if not incomplete:
            continue
        oldest, _ = stalled
        if oldest is None or opened_at < oldest:
            stalled = (opened_at, entry)
    return stalled[1]


def _is_live_trading() -> bool:
    state = runtime.get_state()
    control = state.control
    if getattr(control, "dry_run_mode", False):
        return False
    if getattr(control, "dry_run", False):
        return False
    return True


def _recent_order_rejections(
    *,
    now: datetime,
    burst_threshold: int,
    window_seconds: float,
) -> Optional[Dict[str, object]]:
    if burst_threshold <= 0:
        return None
    entries = hedge_log.read_entries(limit=max(burst_threshold * 3, 20))
    count = 0
    earliest: Optional[datetime] = None
    last_reason = ""
    for entry in reversed(entries):
        if bool(entry.get("dry_run_mode")):
            continue
        if bool(entry.get("simulated")):
            continue
        text = str(entry.get("status") or entry.get("result") or "").strip()
        lowered = text.lower()
        if not lowered:
            if count == 0:
                continue
            break
        failure = False
        if lowered.startswith(("rejected", "error", "fail")):
            failure = True
        elif "ban" in lowered or "forbid" in lowered or "blocked" in lowered:
            failure = True
        if not failure:
            if count == 0:
                continue
            break
        ts = _parse_timestamp(entry.get("timestamp") or entry.get("ts"))
        if ts is None:
            continue
        if window_seconds > 0 and (now - ts).total_seconds() > window_seconds:
            break
        count += 1
        last_reason = text
        if earliest is None or ts < earliest:
            earliest = ts
        if count >= burst_threshold:
            return {
                "count": count,
                "since": earliest.isoformat() if earliest else None,
                "latest_reason": last_reason,
                "window_seconds": window_seconds,
            }
    return None


def force_hold(reason: str, *, extra: Mapping[str, object] | None = None) -> bool:
    """Engage HOLD with an auto-throttle reason and log the incident."""

    reason_text = _normalise_reason(reason)
    safety_snapshot = runtime.get_safety_status()
    hold_before = bool(safety_snapshot.get("hold_active"))
    previous_reason = str(safety_snapshot.get("hold_reason") or "")

    runtime.engage_safety_hold(reason_text, source="risk_guard")

    resume_snapshot = runtime.record_resume_request(
        f"{reason_text} — manual review required",
        requested_by="risk_guard",
    )

    triggered = not hold_before or previous_reason != reason_text
    if triggered:
        extra_payload = dict(extra) if isinstance(extra, Mapping) else {}
        notifier.emit_alert(
            "risk_guard_force_hold",
            f"Risk throttle engaged: {reason_text}",
            extra={
                "reason": reason_text,
                "details": extra_payload,
                "resume_request": resume_snapshot,
            },
            active=True,
            alert_id=reason_text,
        )
    return triggered


def evaluate(*, now: Optional[datetime] = None) -> List[str]:
    """Evaluate live risk signals and engage HOLD on hard breaches."""

    evaluation_ts = now or datetime.now(timezone.utc)
    triggered: List[str] = []

    records = list_position_records()
    active_positions = _active_positions(records)

    max_total = _env_float("MAX_TOTAL_NOTIONAL_USDT", 0.0)
    if max_total > 0:
        current_total = _total_notional_usdt(active_positions)
        if current_total > max_total:
            details = {
                "current_notional_usdt": float(round(current_total, 6)),
                "limit_notional_usdt": float(max_total),
            }
            if force_hold(REASON_RUNAWAY_NOTIONAL, extra=details):
                triggered.append(REASON_RUNAWAY_NOTIONAL)
                return triggered

    max_open = _env_int("MAX_OPEN_POSITIONS", 0)
    if max_open > 0:
        open_count = len(active_positions)
        if open_count > max_open:
            details = {"open_positions": open_count, "limit": int(max_open)}
            if force_hold(REASON_RUNAWAY_POSITIONS, extra=details):
                triggered.append(REASON_RUNAWAY_POSITIONS)
                return triggered

    partial_threshold = _env_float(
        "PARTIAL_HEDGE_THROTTLE_SECONDS", _env_float("PARTIAL_HEDGE_ALERT_SECONDS", 300.0)
    )
    stalled = _partial_stalled(
        active_positions, now=evaluation_ts, threshold_seconds=partial_threshold
    )
    if stalled:
        age_seconds = 0
        opened_at = _parse_timestamp(stalled.get("timestamp"))
        if opened_at:
            age_seconds = int((evaluation_ts - opened_at).total_seconds())
        details = {
            "position_id": stalled.get("id"),
            "symbol": stalled.get("symbol"),
            "age_seconds": age_seconds,
        }
        if force_hold(REASON_PARTIAL_STALLED, extra=details):
            triggered.append(REASON_PARTIAL_STALLED)
            return triggered

    auto_state = runtime.get_auto_hedge_state()
    failure_threshold = _env_int(
        "RISK_GUARD_AUTO_HEDGE_FAILURES", max(_env_int("MAX_AUTO_FAILS_PER_MIN", 3), 3)
    )
    consecutive_failures = int(getattr(auto_state, "consecutive_failures", 0) or 0)
    if failure_threshold > 0 and consecutive_failures >= failure_threshold:
        details = {
            "consecutive_failures": consecutive_failures,
            "threshold": failure_threshold,
            "last_result": getattr(auto_state, "last_execution_result", ""),
        }
        if force_hold(REASON_AUTO_HEDGE_FAILURES, extra=details):
            triggered.append(REASON_AUTO_HEDGE_FAILURES)
            return triggered

    if _is_live_trading():
        rejection_threshold = _env_int("RISK_GUARD_REJECTION_BURST", 5)
        window_seconds = _env_float("RISK_GUARD_REJECTION_WINDOW_SECONDS", 300.0)
        rejection_details = _recent_order_rejections(
            now=evaluation_ts,
            burst_threshold=rejection_threshold,
            window_seconds=window_seconds,
        )
        if rejection_details:
            if force_hold(REASON_ORDER_REJECTIONS, extra=rejection_details):
                triggered.append(REASON_ORDER_REJECTIONS)
                return triggered

    return triggered


__all__ = [
    "AUTO_THROTTLE_PREFIX",
    "REASON_AUTO_HEDGE_FAILURES",
    "REASON_ORDER_REJECTIONS",
    "REASON_PARTIAL_STALLED",
    "REASON_RUNAWAY_NOTIONAL",
    "REASON_RUNAWAY_POSITIONS",
    "evaluate",
    "force_hold",
]
