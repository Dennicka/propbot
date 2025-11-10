from __future__ import annotations

import asyncio
import logging
import os
import time
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

from prometheus_client import Counter, Gauge

from .. import ledger
from ..services import runtime
from ..services.market_ws import market_status_snapshot
from ..telemetry.metrics import slo_snapshot
from ..watchdog.core import STATE_DOWN as BROKER_DOWN
from ..watchdog.core import STATE_UP as BROKER_UP
from ..watchdog.core import get_broker_state
from ..watchdog.exchange_watchdog import get_exchange_watchdog
from ..recon.reconciler import RECON_QTY_TOL
from ..slo.guard import build_default_context as build_slo_context

logger = logging.getLogger("propbot.readiness")


class ReadinessStatus(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


READINESS_STATUS_GAUGE = Gauge(
    "readiness_status",
    "Aggregated live readiness status",
    ("status",),
)
READINESS_REASON_TOTAL = Counter(
    "readiness_reason_total",
    "Total count of readiness blocking reasons",
    ("reason",),
)

DEFAULT_POLL_INTERVAL = 1.0
_DETAIL_KEYS = (
    "config_loaded",
    "db_ok",
    "metrics_ok",
    "md_connected",
    "md_staleness_ok",
    "watchdog_state",
    "recon_divergence_ok",
    "pretrade_throttled",
    "risk_throttled",
    "router_ready",
    "state",
)
_BOOL_OK_FLAGS = (
    ("config_loaded", "config_not_loaded"),
    ("db_ok", "db_unavailable"),
    ("metrics_ok", "metrics_unavailable"),
    ("md_connected", "md_disconnected"),
    ("md_staleness_ok", "md_staleness"),
    ("recon_divergence_ok", "recon_divergence"),
    ("router_ready", "router_not_ready"),
)
_BOOL_ALERT_FLAGS = (
    ("pretrade_throttled", "pretrade_throttled"),
    ("risk_throttled", "risk_throttled"),
)
_MISSING_PREFIX = "missing_signal::"


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _normalise_state(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def _normalise_context(ctx: Mapping[str, Any] | Any) -> dict[str, Any]:
    details: dict[str, Any] = {}
    mapping: Mapping[str, Any] | None = ctx if isinstance(ctx, Mapping) else None
    for key in _DETAIL_KEYS:
        if mapping is not None and key in mapping:
            raw_value = mapping[key]
        else:
            raw_value = getattr(ctx, key, None)
        if key == "watchdog_state":
            details[key] = _normalise_state(raw_value)
        elif key == "state":
            state_value = _normalise_state(raw_value)
            details[key] = state_value
        else:
            details[key] = _coerce_bool(raw_value)
    return details


class LiveReadinessAggregator:
    def __init__(self) -> None:
        self._status_gauges: MutableMapping[ReadinessStatus, Gauge] = {
            status: READINESS_STATUS_GAUGE.labels(status=status.value) for status in ReadinessStatus
        }
        for gauge in self._status_gauges.values():
            gauge.set(0.0)
        self._last_status: ReadinessStatus | None = None
        self._last_reasons: tuple[str, ...] = ()

    def snapshot(self, ctx: Mapping[str, Any] | Any) -> dict[str, Any]:
        details = _normalise_context(ctx)
        reasons, status = _classify(details)
        self._update_metrics(status, reasons)
        payload = {
            "status": status.value,
            "reasons": reasons,
            "details": details,
        }
        return payload

    def _update_metrics(self, status: ReadinessStatus, reasons: Sequence[str]) -> None:
        for label, gauge in self._status_gauges.items():
            gauge.set(1.0 if label == status else 0.0)
        reasons_tuple = tuple(reasons)
        if reasons_tuple != self._last_reasons:
            for reason in reasons:
                READINESS_REASON_TOTAL.labels(reason=reason).inc()
            self._last_reasons = reasons_tuple
        self._last_status = status


def _classify(details: Mapping[str, Any]) -> tuple[list[str], ReadinessStatus]:
    red = False
    yellow = False
    reasons: list[str] = []

    for key, reason in _BOOL_OK_FLAGS:
        value = details.get(key)
        if value is None:
            reasons.append(f"{_MISSING_PREFIX}{key}")
            red = True
        elif not bool(value):
            reasons.append(reason)
            red = True

    for key, reason in _BOOL_ALERT_FLAGS:
        value = details.get(key)
        if value is None:
            reasons.append(f"{_MISSING_PREFIX}{key}")
            red = True
        elif bool(value):
            reasons.append(reason)
            red = True

    watchdog_state = details.get("watchdog_state")
    if watchdog_state is None:
        reasons.append(f"{_MISSING_PREFIX}watchdog_state")
        red = True
    else:
        state_value = str(watchdog_state).upper()
        if state_value == "DOWN":
            reasons.append("watchdog_down")
            red = True
        elif state_value == "DEGRADED":
            reasons.append("watchdog_degraded")
            yellow = True

    mode = details.get("state")
    if mode is None:
        reasons.append(f"{_MISSING_PREFIX}state")
        red = True
    else:
        mode_value = str(mode).upper()
        if mode_value == "HOLD":
            reasons.append("manual_hold")
            yellow = True

    reasons = _unique(reasons)
    if red:
        return reasons, ReadinessStatus.RED
    if yellow:
        return reasons, ReadinessStatus.YELLOW
    return reasons, ReadinessStatus.GREEN


def _md_staleness_threshold() -> float:
    try:
        ctx = build_slo_context()
        config = getattr(ctx, "config", None)
        value = getattr(config, "md_staleness_critical", None)
        if value is not None:
            return max(float(value), 0.0)
    except Exception:  # pragma: no cover - defensive
        logger.debug("readiness: failed to resolve staleness threshold", exc_info=True)
    raw = os.getenv("SLO_MD_STALENESS_CRITICAL_S")
    if raw:
        try:
            return max(float(raw), 0.0)
        except ValueError:
            logger.debug("invalid SLO_MD_STALENESS_CRITICAL_S=%s", raw)
    return 5.0


def _resolve_recon_threshold(state: Any) -> float:
    config = getattr(getattr(state, "config", None), "data", None)
    if config is not None:
        recon_cfg = getattr(config, "recon", None)
        if recon_cfg is not None:
            value = getattr(recon_cfg, "max_divergence", None)
            if value is not None:
                try:
                    return max(float(value), 0.0)
                except (TypeError, ValueError):
                    logger.debug("readiness: recon.max_divergence invalid: %r", value)
    raw = os.getenv("RECON_MAX_DIVERGENCE")
    if raw:
        try:
            return max(float(raw), 0.0)
        except ValueError:
            logger.debug("invalid RECON_MAX_DIVERGENCE=%s", raw)
    return float(RECON_QTY_TOL)


def _latest_recon_divergence(threshold: float, snapshot: Mapping[str, Any] | None) -> bool | None:
    if not isinstance(snapshot, Mapping):
        return None
    diffs = snapshot.get("diffs")
    if diffs is None:
        return True
    if not isinstance(diffs, Sequence):
        return None
    if not diffs:
        return True
    max_delta = 0.0
    missing_delta = False
    for entry in diffs:
        if not isinstance(entry, Mapping):
            missing_delta = True
            continue
        delta_raw = entry.get("delta")
        try:
            delta_value = abs(float(delta_raw))
        except (TypeError, ValueError):
            missing_delta = True
            continue
        if delta_value > max_delta:
            max_delta = delta_value
    if missing_delta:
        return False
    return max_delta <= threshold


def _resolve_watchdog_state() -> str | None:
    watchdog = get_exchange_watchdog()
    snapshot = watchdog.get_state()
    if not isinstance(snapshot, Mapping) or not snapshot:
        return "OK" if watchdog.overall_ok() else "DEGRADED"
    overall_state = "OK"
    for payload in snapshot.values():
        if not isinstance(payload, Mapping):
            continue
        status = str(payload.get("status") or "").strip().upper() or None
        if status == "AUTO_HOLD":
            return "DOWN"
        if status and status != "OK":
            overall_state = "DEGRADED"
    if overall_state == "OK" and not watchdog.overall_ok():
        overall_state = "DEGRADED"
    return overall_state


def _resolve_router_ready() -> bool | None:
    try:
        broker_state = get_broker_state()
    except Exception:  # pragma: no cover - defensive
        logger.debug("readiness: failed to fetch broker state", exc_info=True)
        return None
    overall = getattr(broker_state, "overall", None)
    if overall is None:
        return None
    state_value = getattr(overall, "state", None)
    if state_value is None:
        return None
    state_text = str(state_value).upper()
    if state_text == BROKER_UP:
        return True
    if state_text == BROKER_DOWN:
        return False
    return False


def _resolve_metrics_ok() -> bool | None:
    try:
        snapshot = slo_snapshot()
    except Exception:  # pragma: no cover - defensive
        logger.debug("readiness: slo_snapshot failed", exc_info=True)
        return False
    return bool(isinstance(snapshot, Mapping))


def _resolve_md_signals(threshold: float) -> tuple[bool | None, bool | None]:
    try:
        status_rows = market_status_snapshot()
    except Exception:  # pragma: no cover - defensive
        logger.debug("readiness: market_status_snapshot failed", exc_info=True)
        return None, None
    if not isinstance(status_rows, Sequence) or not status_rows:
        return False, False
    connected = True
    staleness_values: list[float] = []
    for row in status_rows:
        if not isinstance(row, Mapping):
            continue
        state_text = str(row.get("state") or "").strip().upper()
        if state_text == "DOWN":
            connected = False
        staleness = row.get("staleness_s")
        try:
            staleness_values.append(float(staleness))
        except (TypeError, ValueError):
            continue
    if not staleness_values:
        return connected, False if connected else connected
    max_staleness = max(staleness_values)
    return connected, max_staleness <= threshold


def _resolve_db_ok() -> bool | None:
    try:
        return ledger.LEDGER_PATH.exists()
    except Exception:  # pragma: no cover - defensive
        logger.debug("readiness: ledger path check failed", exc_info=True)
        return None


def collect_readiness_signals() -> dict[str, Any]:
    details: dict[str, Any] = {key: None for key in _DETAIL_KEYS}
    try:
        state = runtime.get_state()
    except Exception:  # pragma: no cover - defensive
        logger.debug("readiness: runtime state unavailable", exc_info=True)
        state = None

    if state is not None:
        config_loaded = getattr(state, "config", None)
        details["config_loaded"] = bool(config_loaded)
        control = getattr(state, "control", None)
        details["state"] = _normalise_state(getattr(control, "mode", None))
        pre_trade_gate = getattr(state, "pre_trade_gate", None)
        if pre_trade_gate is not None:
            details["pretrade_throttled"] = bool(getattr(pre_trade_gate, "is_throttled", False))
        safety = getattr(state, "safety", None)
        if safety is not None:
            details["risk_throttled"] = bool(getattr(safety, "risk_throttled", False))
        threshold = _resolve_recon_threshold(state)
        recon_snapshot = runtime.get_reconciliation_status()
        details["recon_divergence_ok"] = _latest_recon_divergence(threshold, recon_snapshot)
    else:
        threshold = float(RECON_QTY_TOL)

    details["db_ok"] = _resolve_db_ok()
    details["metrics_ok"] = _resolve_metrics_ok()
    md_threshold = _md_staleness_threshold()
    md_connected, md_fresh = _resolve_md_signals(md_threshold)
    details["md_connected"] = md_connected
    details["md_staleness_ok"] = md_fresh
    details["watchdog_state"] = _resolve_watchdog_state()
    details["router_ready"] = _resolve_router_ready()

    return details


READINESS_AGGREGATOR = LiveReadinessAggregator()


async def wait_for_live_readiness(
    aggregator: LiveReadinessAggregator,
    ctx_factory: Callable[[], Mapping[str, Any] | Any],
    *,
    interval_s: float = DEFAULT_POLL_INTERVAL,
    timeout_s: float = 120.0,
    log: logging.Logger | None = None,
) -> bool:
    logger_obj = log or logger
    start = time.monotonic()
    while True:
        try:
            context = ctx_factory()
        except Exception:  # pragma: no cover - defensive
            logger_obj.exception("wait-for-readiness: context factory failed")
            context = {}
        snapshot = aggregator.snapshot(context)
        status = snapshot.get("status", "UNKNOWN")
        reasons = snapshot.get("reasons", [])
        logger_obj.info("wait-for-readiness: status=%s reasons=%s", status, reasons)
        if status == ReadinessStatus.GREEN.value:
            return True
        if time.monotonic() - start >= timeout_s:
            return False
        await asyncio.sleep(max(interval_s, 0.01))


__all__ = [
    "DEFAULT_POLL_INTERVAL",
    "LiveReadinessAggregator",
    "READINESS_AGGREGATOR",
    "ReadinessStatus",
    "collect_readiness_signals",
    "wait_for_live_readiness",
]
