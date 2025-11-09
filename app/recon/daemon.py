"""Background reconciliation loop responsible for monitoring diffs."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal, Mapping

from ..audit_log import log_operator_action
from ..broker.router import ExecutionRouter
from ..metrics.recon import RECON_DIFF_ABS_USD_GAUGE, RECON_DIFF_STATE_GAUGE
from ..services import runtime
from ..risk.freeze import FreezeRule, get_freeze_registry
from .service import ReconDiff, collect_recon_snapshot

LOGGER = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SEC = 5.0
_ACTIVE_LABELS: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class ReconThresholds:
    abs_warn: float
    abs_crit: float
    rel_warn: float
    rel_crit: float


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_thresholds() -> ReconThresholds:
    state = runtime.get_state()
    config = getattr(state, "config", None)
    data = getattr(config, "data", None)
    recon_cfg = getattr(data, "recon", None)
    abs_warn = float(getattr(recon_cfg, "diff_abs_usd_warn", 50.0) or 50.0)
    abs_crit = float(getattr(recon_cfg, "diff_abs_usd_crit", max(abs_warn * 2, abs_warn)) or abs_warn)
    if abs_crit < abs_warn:
        abs_crit = abs_warn
    rel_warn = float(getattr(recon_cfg, "diff_rel_warn", 0.05) or 0.05)
    rel_crit = float(getattr(recon_cfg, "diff_rel_crit", max(rel_warn * 2, rel_warn)) or rel_warn)
    if rel_crit < rel_warn:
        rel_crit = rel_warn
    return ReconThresholds(abs_warn=abs_warn, abs_crit=abs_crit, rel_warn=rel_warn, rel_crit=rel_crit)


def _classify(diff: ReconDiff, thresholds: ReconThresholds) -> str:
    severity = "OK"
    rel = diff.diff_rel or 0.0
    if diff.diff_abs >= thresholds.abs_crit or rel >= thresholds.rel_crit:
        severity = "CRIT"
    elif diff.diff_abs >= thresholds.abs_warn or rel >= thresholds.rel_warn:
        severity = "WARN"
    return severity


async def _fetch_remote_balances(state) -> list[dict[str, object]]:
    runtime_deriv = getattr(state, "derivatives", None)
    venues = getattr(runtime_deriv, "venues", {}) if runtime_deriv else {}
    if not venues:
        return []
    router = ExecutionRouter()
    tasks = []
    for venue_id in venues.keys():
        venue = venue_id.replace("_", "-")
        broker = router.broker_for_venue(venue)
        tasks.append(asyncio.create_task(broker.balances(venue=venue)))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    balances: list[dict[str, object]] = []
    for venue_id, result in zip(venues.keys(), results):
        venue = venue_id.replace("_", "-")
        if isinstance(result, Exception):
            LOGGER.warning("recon.remote_balances_failed", extra={"venue": venue, "error": str(result)})
            continue
        payload = result.get("balances") if isinstance(result, Mapping) else None
        if isinstance(payload, list):
            balances.extend([row for row in payload if isinstance(row, Mapping)])
    return balances


def _set_metrics(diff: ReconDiff, severity: str) -> tuple[str, str]:
    venue = (diff.venue or "").lower() or "unknown"
    symbol = (diff.symbol or "").upper() or "UNKNOWN"
    RECON_DIFF_ABS_USD_GAUGE.labels(venue=venue, symbol=symbol).set(float(diff.diff_abs))
    for state in ("OK", "WARN", "CRIT"):
        value = 1.0 if severity == state else 0.0
        RECON_DIFF_STATE_GAUGE.labels(venue=venue, symbol=symbol, state=state).set(value)
    return venue, symbol


def _reset_missing_metric_labels(active: set[tuple[str, str]]) -> None:
    stale = _ACTIVE_LABELS - active
    for venue, symbol in stale:
        RECON_DIFF_ABS_USD_GAUGE.labels(venue=venue, symbol=symbol).set(0.0)
        for state in ("OK", "WARN", "CRIT"):
            RECON_DIFF_STATE_GAUGE.labels(venue=venue, symbol=symbol, state=state).set(1.0 if state == "OK" else 0.0)
    _ACTIVE_LABELS.clear()
    _ACTIVE_LABELS.update(active)


def _apply_recon_freeze(diff: ReconDiff) -> None:
    registry = get_freeze_registry()
    venue_token = (diff.venue or "").strip().lower()
    symbol_token = (diff.symbol or "").strip().upper()
    scope: Literal["symbol", "venue"]
    scope = "symbol" if symbol_token else "venue"
    reason_parts = ["RECON_CRITICAL"]
    if venue_token:
        reason_parts.append(f"venue={venue_token}")
    if scope == "symbol" and symbol_token:
        reason_parts.append(f"symbol={symbol_token}")
    reason = "::".join(reason_parts)
    rule = FreezeRule(reason=reason, scope=scope, ts=time.time())
    if registry.apply(rule):
        log_operator_action(
            "system",
            "system",
            "AUTO_FREEZE_APPLIED",
            {
                "source": "recon",
                "reason": reason,
                "scope": scope,
                "venue": diff.venue,
                "symbol": diff.symbol,
            },
        )


async def _build_context(state, remote_balances: list[dict[str, object]]) -> SimpleNamespace:
    runtime_ns = SimpleNamespace(remote_balances=lambda: remote_balances)
    ctx = SimpleNamespace(runtime=runtime_ns)
    return ctx


async def run_recon_cycle(
    thresholds: ReconThresholds | None = None,
    *,
    enable_hold: bool | None = None,
) -> dict[str, object]:
    state = runtime.get_state()
    remote_balances = await _fetch_remote_balances(state)
    ctx = await _build_context(state, remote_balances)
    thresholds = thresholds or _resolve_thresholds()
    if enable_hold is None:
        enable_hold = _env_flag("ENABLE_RECON_HOLD", False)
    auto_freeze_enabled = _env_flag("AUTO_FREEZE_ON_RECON", False)

    try:
        diffs = collect_recon_snapshot(ctx)
    except Exception:
        LOGGER.exception("recon.snapshot_failed")
        raise

    active_labels: set[tuple[str, str]] = set()
    has_warn = False
    has_crit = False
    diff_payloads: list[dict[str, object]] = []

    for diff in diffs:
        severity = _classify(diff, thresholds)
        active_labels.add(_set_metrics(diff, severity))
        payload = asdict(diff)
        payload["severity"] = severity
        diff_payloads.append(payload)
        if severity == "CRIT":
            has_crit = True
            LOGGER.error("recon.diff_critical", extra=payload)
            if auto_freeze_enabled:
                _apply_recon_freeze(diff)
        elif severity == "WARN":
            has_warn = True
            LOGGER.warning("recon.diff_warning", extra=payload)

    _reset_missing_metric_labels(active_labels)

    snapshot_state = "CRIT" if has_crit else "WARN" if has_warn else "OK"
    metadata = {
        "state": snapshot_state,
        "status": snapshot_state,
        "has_warn": has_warn,
        "has_crit": has_crit,
        "last_checked": _iso_now(),
    }
    metadata["auto_hold"] = bool(enable_hold)
    runtime.update_reconciliation_status(diffs=diff_payloads, metadata=metadata, desync_detected=bool(diffs))

    if has_crit:
        details = {"diffs": diff_payloads, "timestamp": metadata["last_checked"]}
        log_operator_action("system", "system", "RECON_CRITICAL", details)
        if enable_hold:
            try:
                runtime.flag_recon_issue("CRITICAL", details=details)
            except Exception:  # pragma: no cover - defensive
                LOGGER.exception("recon.flag_issue_failed")
    elif auto_freeze_enabled:
        get_freeze_registry().clear("RECON_CRITICAL")

    return {
        "diffs": diff_payloads,
        "has_warn": has_warn,
        "has_crit": has_crit,
        "state": snapshot_state,
    }


async def run_recon_loop(interval: float | None = None) -> None:
    interval_value = interval if interval is not None else _env_float("RECON_LOOP_INTERVAL_SEC", _DEFAULT_INTERVAL_SEC)
    LOGGER.info("recon.loop_start", extra={"interval": interval_value})
    while True:
        start = time.perf_counter()
        thresholds = _resolve_thresholds()
        enable_hold = _env_flag("ENABLE_RECON_HOLD", False)
        try:
            await run_recon_cycle(thresholds, enable_hold=enable_hold)
        except Exception:
            await asyncio.sleep(max(interval_value, 0.5))
            continue
        elapsed = time.perf_counter() - start
        await asyncio.sleep(max(interval_value - elapsed, 0.5))


__all__ = ["run_recon_loop", "run_recon_cycle", "ReconThresholds"]

