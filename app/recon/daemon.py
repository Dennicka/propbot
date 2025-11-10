"""Reconciliation daemon responsible for periodic position/balance checks."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Mapping, Sequence

from ..audit_log import log_operator_action
from ..broker.router import ExecutionRouter
from ..golden.logger import get_golden_logger
from ..metrics.recon import (
    RECON_AUTO_HOLD_COUNTER,
    RECON_DIFF_NOTIONAL_GAUGE,
    RECON_STATUS_GAUGE,
)
from ..services import runtime
from .core import ReconSettings, ReconSnapshot, reconcile_once


LOGGER = logging.getLogger(__name__)

_ACTIVE_DIFF_LABELS: set[tuple[str, str, str]] = set()
_ACTIVE_STATUS_LABELS: set[tuple[str, str]] = set()


@dataclass(slots=True)
class DaemonConfig:
    enabled: bool = True
    interval_sec: float = 15.0
    warn_notional_usd: Decimal = Decimal("5")
    critical_notional_usd: Decimal = Decimal("25")
    clear_after_ok_runs: int = 3


class ReconDaemon:
    """Manage reconciliation cycles and associated safety reactions."""

    def __init__(self, config: DaemonConfig | None = None) -> None:
        self._config = config or _resolve_daemon_config()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._consecutive_ok = 0
        self._auto_hold_active = False
        self._previous_safe_mode: bool | None = None

    @property
    def auto_hold_active(self) -> bool:
        return self._auto_hold_active

    async def start(self) -> None:
        if not self._config.enabled:
            LOGGER.info("recon.daemon_disabled")
            return
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:  # pragma: no cover - lifecycle cleanup
            return
        finally:
            self._task = None

    async def run_once(self) -> list[ReconSnapshot]:
        """Execute a single reconciliation cycle."""

        state = runtime.get_state()
        remote_balances = await _fetch_remote_balances(state)
        settings = ReconSettings(
            warn_notional_usd=self._config.warn_notional_usd,
            critical_notional_usd=self._config.critical_notional_usd,
        )
        recon_ctx = SimpleNamespace(
            cfg=SimpleNamespace(recon=settings),
            remote_balances=lambda: remote_balances,
            runtime=SimpleNamespace(
                get_state=runtime.get_state,
                remote_balances=lambda: remote_balances,
            ),
        )

        snapshots = reconcile_once(recon_ctx)
        self._publish_results(snapshots)
        return snapshots

    async def _run_loop(self) -> None:
        LOGGER.info("recon.daemon_start", extra={"interval": self._config.interval_sec})
        while not self._stop_event.is_set():
            start = time.perf_counter()
            try:
                await self.run_once()
            except Exception:
                LOGGER.exception("recon.daemon_cycle_failed")
            elapsed = time.perf_counter() - start
            wait_for = max(self._config.interval_sec - elapsed, 0.5)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_for)
            except asyncio.TimeoutError:
                continue
        LOGGER.info("recon.daemon_stop")

    def _publish_results(self, snapshots: Sequence[ReconSnapshot]) -> None:
        worst_state = "OK"
        venue_states: dict[str, str] = {}
        diff_labels: set[tuple[str, str, str]] = set()
        now_iso = datetime.now(timezone.utc).isoformat()

        for snapshot in snapshots:
            payload = _snapshot_payload(snapshot)
            LOGGER.info("recon.snapshot", extra=payload)
            venue_states[snapshot.venue] = _worst_state(
                venue_states.get(snapshot.venue, "OK"), snapshot.status
            )
            worst_state = _worst_state(worst_state, snapshot.status)
            label_symbol = snapshot.symbol or snapshot.asset
            diff_labels.add((snapshot.venue, label_symbol, snapshot.status))
            RECON_DIFF_NOTIONAL_GAUGE.labels(
                venue=snapshot.venue,
                symbol=label_symbol,
                status=snapshot.status,
            ).set(float(snapshot.diff_abs))

        _reset_diff_metrics(diff_labels)
        _update_status_metrics(venue_states)
        self._handle_auto_hold(worst_state)

        metadata = {
            "state": worst_state,
            "status": worst_state,
            "last_checked": now_iso,
            "last_ts": max((snapshot.ts for snapshot in snapshots), default=time.time()),
            "auto_hold": self._auto_hold_active,
        }
        runtime.update_reconciliation_status(
            diffs=[_snapshot_payload(snapshot) for snapshot in snapshots],
            metadata=metadata,
        )
        self._log_summary(worst_state, snapshots)

    def _handle_auto_hold(self, worst_state: str) -> None:
        if worst_state == "CRITICAL":
            self._consecutive_ok = 0
            self._engage_auto_hold()
            return
        if worst_state == "WARN":
            self._consecutive_ok = 0
            return
        self._consecutive_ok += 1
        if (
            self._auto_hold_active
            and self._consecutive_ok >= self._config.clear_after_ok_runs
        ):
            self._release_auto_hold()

    def _engage_auto_hold(self) -> None:
        state = runtime.get_state()
        safety = getattr(state, "safety", None)
        control = getattr(state, "control", None)
        if safety is None or control is None:
            return
        if not self._auto_hold_active:
            self._previous_safe_mode = bool(getattr(control, "safe_mode", True))
        engaged = runtime.engage_safety_hold("RECON_DIVERGENCE", source="recon")
        if engaged:
            self._auto_hold_active = True
            RECON_AUTO_HOLD_COUNTER.inc()
            log_operator_action(
                "system",
                "system",
                "RECON_AUTO_HOLD",
                {"reason": "RECON_DIVERGENCE"},
            )
            LOGGER.error("recon.auto_hold_engaged")

    def _release_auto_hold(self) -> None:
        state = runtime.get_state()
        safety = getattr(state, "safety", None)
        if safety is None or not getattr(safety, "hold_active", False):
            self._auto_hold_active = False
            self._previous_safe_mode = None
            return
        reason = str(getattr(safety, "hold_reason", ""))
        if not reason.upper().startswith("RECON_DIVERGENCE"):
            return
        safe_mode = self._previous_safe_mode
        if safe_mode is None:
            control = getattr(state, "control", None)
            safe_mode = bool(getattr(control, "safe_mode", True))
        runtime.autopilot_apply_resume(safe_mode=safe_mode)
        self._auto_hold_active = False
        self._previous_safe_mode = None
        LOGGER.info("recon.auto_hold_cleared")

    def _log_summary(self, worst_state: str, snapshots: Sequence[ReconSnapshot]) -> None:
        logger = get_golden_logger()
        if not logger.enabled:
            return
        logger.log(
            "recon_guard",
            {
                "state": worst_state,
                "diffs": [
                    {
                        "venue": snapshot.venue,
                        "symbol": snapshot.symbol or snapshot.asset,
                        "status": snapshot.status,
                        "diff_abs": float(snapshot.diff_abs),
                    }
                    for snapshot in snapshots
                ],
            },
        )


async def _fetch_remote_balances(state) -> list[Mapping[str, object]]:
    runtime_deriv = getattr(state, "derivatives", None)
    venues = getattr(runtime_deriv, "venues", {}) if runtime_deriv else {}
    if not venues:
        return []
    router = ExecutionRouter()
    tasks: list[asyncio.Task] = []
    for venue_id in venues.keys():
        venue = venue_id.replace("_", "-")
        broker = router.broker_for_venue(venue)
        tasks.append(asyncio.create_task(broker.balances(venue=venue)))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    balances: list[Mapping[str, object]] = []
    for venue_id, result in zip(venues.keys(), results):
        venue = venue_id.replace("_", "-")
        if isinstance(result, Exception):
            LOGGER.warning(
                "recon.remote_balances_failed",
                extra={"venue": venue, "error": str(result)},
            )
            continue
        payload = result.get("balances") if isinstance(result, Mapping) else None
        if isinstance(payload, Sequence):
            balances.extend([row for row in payload if isinstance(row, Mapping)])
    return balances


def _reset_diff_metrics(active: set[tuple[str, str, str]]) -> None:
    stale = _ACTIVE_DIFF_LABELS - active
    for venue, symbol, status in stale:
        RECON_DIFF_NOTIONAL_GAUGE.labels(venue=venue, symbol=symbol, status=status).set(0.0)
    _ACTIVE_DIFF_LABELS.clear()
    _ACTIVE_DIFF_LABELS.update(active)


def _update_status_metrics(venue_states: Mapping[str, str]) -> None:
    active: set[tuple[str, str]] = set()
    for venue, status in venue_states.items():
        for candidate in ("OK", "WARN", "CRITICAL"):
            value = 1.0 if candidate == status else 0.0
            RECON_STATUS_GAUGE.labels(venue=venue, status=candidate).set(value)
            active.add((venue, candidate))
    stale = _ACTIVE_STATUS_LABELS - active
    for venue, status in stale:
        value = 1.0 if status == "OK" else 0.0
        RECON_STATUS_GAUGE.labels(venue=venue, status=status).set(value)
    _ACTIVE_STATUS_LABELS.clear()
    _ACTIVE_STATUS_LABELS.update(active)


def _worst_state(current: str, candidate: str) -> str:
    order = {"OK": 0, "WARN": 1, "CRITICAL": 2}
    current_rank = order.get(current, 0)
    candidate_rank = order.get(candidate, 0)
    return current if current_rank >= candidate_rank else candidate


def _snapshot_payload(snapshot: ReconSnapshot) -> dict[str, object]:
    return {
        "event": "recon_snapshot",
        "venue": snapshot.venue,
        "asset": snapshot.asset,
        "symbol": snapshot.symbol,
        "side": snapshot.side,
        "exch_position": _decimal_to_str(snapshot.exch_position),
        "local_position": _decimal_to_str(snapshot.local_position),
        "exch_balance": _decimal_to_str(snapshot.exch_balance),
        "local_balance": _decimal_to_str(snapshot.local_balance),
        "diff_abs": _decimal_to_str(snapshot.diff_abs),
        "status": snapshot.status,
        "reason": snapshot.reason,
        "ts": snapshot.ts,
    }


def _decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _resolve_daemon_config(cfg: object | None = None) -> DaemonConfig:
    state = runtime.get_state()
    config_data = getattr(getattr(state, "config", None), "data", None)
    recon_cfg = getattr(config_data, "recon", None)
    if cfg is not None:
        recon_cfg = getattr(cfg, "recon", recon_cfg)
    if recon_cfg is None:
        return DaemonConfig()
    enabled = bool(getattr(recon_cfg, "enabled", True))
    interval = float(getattr(recon_cfg, "interval_sec", 15.0) or 15.0)
    warn = getattr(recon_cfg, "warn_notional_usd", getattr(recon_cfg, "diff_abs_usd_warn", 5.0))
    crit = getattr(
        recon_cfg,
        "critical_notional_usd",
        getattr(recon_cfg, "diff_abs_usd_crit", max(float(warn) * 2, float(warn))),
    )
    clear_runs = int(getattr(recon_cfg, "clear_after_ok_runs", 3) or 3)
    return DaemonConfig(
        enabled=enabled,
        interval_sec=interval,
        warn_notional_usd=Decimal(str(warn)),
        critical_notional_usd=Decimal(str(crit)),
        clear_after_ok_runs=max(1, clear_runs),
    )


async def run_recon_cycle(*, config: DaemonConfig | None = None) -> dict[str, object]:
    daemon = ReconDaemon(config)
    snapshots = await daemon.run_once()
    worst = "OK"
    for snapshot in snapshots:
        worst = _worst_state(worst, snapshot.status)
    return {
        "snapshots": [_snapshot_payload(snapshot) for snapshot in snapshots],
        "worst_state": worst,
        "auto_hold": daemon.auto_hold_active,
    }


async def run_recon_loop(interval: float | None = None) -> None:
    config = _resolve_daemon_config()
    if interval is not None:
        config.interval_sec = interval
    daemon = ReconDaemon(config)
    await daemon.start()
    if daemon._task is not None:
        await daemon._task


def start_recon_daemon(ctx: object | None, cfg: object | None) -> ReconDaemon:
    """Initialise and return a running reconciliation daemon."""

    config = _resolve_daemon_config(cfg)
    daemon = ReconDaemon(config)
    asyncio.create_task(daemon.start())
    return daemon


__all__ = [
    "ReconDaemon",
    "DaemonConfig",
    "run_recon_loop",
    "run_recon_cycle",
    "start_recon_daemon",
]

