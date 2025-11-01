"""Helpers for injecting deterministic chaos faults during drills."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from ..services import runtime
from ..watchdog.exchange_watchdog import (
    WatchdogStateTransition,
    get_exchange_watchdog,
)

logger = logging.getLogger(__name__)

SUPPORTED_KINDS: set[str] = {
    "ws_disconnect",
    "rest_429",
    "order_reject",
    "latency_spike_ms",
}


def _normalise_venue(value: object | None) -> str:
    text = str(value or "").strip()
    return text


def _watchdog_key(venue: str) -> str:
    return venue.strip().lower()


def _display_venue(venue: str | None) -> str | None:
    if not venue:
        return None
    text = str(venue).strip()
    return text.upper() or None


def _serialize_transition(transition: WatchdogStateTransition | None) -> dict[str, Any] | None:
    if transition is None:
        return None
    timestamp = datetime.fromtimestamp(transition.timestamp, tz=timezone.utc).isoformat()
    return {
        "previous": transition.previous,
        "current": transition.current,
        "reason": transition.reason,
        "auto_hold": bool(transition.auto_hold),
        "timestamp": timestamp,
    }


def _snapshot_watchdog_entry(venue_key: str) -> dict[str, Any] | None:
    watchdog = get_exchange_watchdog()
    state = watchdog.get_state()
    entry = state.get(venue_key)
    if entry is None:
        return None
    return dict(entry)


def _snapshot_safety() -> dict[str, Any]:
    payload = runtime.get_safety_status()
    payload["hold_active"] = runtime.is_hold_active()
    return payload


def _ensure_mapping(params: Mapping[str, Any] | None) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise ValueError("params must be a mapping")
    return dict(params)


def _require_positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive integer") from None
    if number <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return number


def _require_venue(params: Mapping[str, Any]) -> str:
    venue = _normalise_venue(params.get("venue"))
    if not venue:
        raise ValueError("venue is required for chaos injection")
    return venue


def _apply_watchdog_fault(venue_key: str, *, reason: str) -> WatchdogStateTransition | None:
    watchdog = get_exchange_watchdog()
    logger.info("chaos injector: watchdog fault", extra={"venue": venue_key, "reason": reason})

    def _probe() -> Mapping[str, object]:
        return {venue_key: {"ok": False, "reason": reason}}

    report = watchdog.check_once(_probe)
    return report.transitions.get(venue_key)


def _apply_latency_fault(venue: str, latency_ms: int) -> dict[str, Any]:
    previous = runtime.get_reconciliation_status()
    issues = [dict(entry) for entry in previous.get("issues", [])]
    issues.append({"kind": "latency_spike_ms", "venue": venue, "latency_ms": latency_ms})
    diffs = [dict(entry) for entry in previous.get("diffs", [])]
    metadata = {
        "auto_hold": bool(previous.get("auto_hold")),
        "chaos_latency_ms": latency_ms,
        "chaos_latency_venue": venue,
    }
    snapshot = runtime.update_reconciliation_status(
        desync_detected=True,
        issues=issues,
        diffs=diffs,
        metadata=metadata,
    )
    return snapshot


def inject(kind: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Apply a deterministic fault injection for resiliency drills."""

    payload = _ensure_mapping(params)
    kind_normalised = str(kind or "").strip().lower()
    if not kind_normalised:
        raise ValueError("kind is required")
    if kind_normalised not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported chaos injection kind: {kind}")

    venue_text = _normalise_venue(payload.get("venue"))
    venue_key = _watchdog_key(venue_text) if venue_text else ""
    venue_display = _display_venue(venue_text)
    endpoint = str(payload.get("endpoint") or "").strip()
    details: dict[str, Any] = {}
    watchdog_transition: WatchdogStateTransition | None = None
    watchdog_snapshot: dict[str, Any] | None = None
    reconciliation_snapshot: dict[str, Any] | None = None

    if kind_normalised in {"ws_disconnect", "rest_429"}:
        venue = _require_venue(payload)
        venue_key = _watchdog_key(venue)
        reason = str(payload.get("reason") or kind_normalised).strip()
        if endpoint:
            reason = f"{reason} ({endpoint})"
        watchdog_transition = _apply_watchdog_fault(venue_key, reason=reason)
        watchdog_snapshot = _snapshot_watchdog_entry(venue_key)
        details = {"reason": reason}
    elif kind_normalised == "order_reject":
        venue = _require_venue(payload)
        venue_key = _watchdog_key(venue)
        description = str(payload.get("reason") or "order rejected").strip() or "order rejected"
        hold_reason = f"chaos:{venue_key}:order_reject"
        engaged = runtime.engage_safety_hold(f"{hold_reason} {description}".strip(), source="chaos_injector")
        details = {"hold_engaged": bool(engaged), "reason": description}
        watchdog_snapshot = _snapshot_watchdog_entry(venue_key)
    elif kind_normalised == "latency_spike_ms":
        venue = _require_venue(payload)
        venue_key = _watchdog_key(venue)
        latency_ms = _require_positive_int(payload.get("ms"), field="ms")
        reconciliation_snapshot = _apply_latency_fault(venue_display or venue.upper(), latency_ms)
        details = {"latency_ms": latency_ms}
        watchdog_snapshot = _snapshot_watchdog_entry(venue_key)
    else:  # pragma: no cover - defensive, should never reach due to supported kinds check
        raise ValueError(f"unsupported chaos injection kind: {kind}")

    safety_snapshot = _snapshot_safety()
    control_snapshot = runtime.control_as_dict()
    if reconciliation_snapshot is None:
        reconciliation_snapshot = runtime.get_reconciliation_status()
    if watchdog_snapshot is None and venue_key:
        watchdog_snapshot = _snapshot_watchdog_entry(venue_key)

    transition_payload = _serialize_transition(watchdog_transition)

    response = {
        "kind": kind_normalised,
        "venue": venue_display,
        "details": details or None,
        "watchdog": watchdog_snapshot,
        "watchdog_transition": transition_payload,
        "reconciliation": reconciliation_snapshot,
        "safety": safety_snapshot,
        "control": control_snapshot,
    }
    return response


__all__ = ["inject", "SUPPORTED_KINDS"]
