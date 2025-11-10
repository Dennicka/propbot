"""Periodic reconciliation daemon orchestrating state comparisons."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Mapping, Sequence

from .. import ledger
from ..broker.router import ExecutionRouter
from ..metrics.recon import (
    RECON_AUTO_HOLD_COUNTER,
    RECON_ISSUES_TOTAL,
    RECON_LAST_RUN_TS,
    RECON_LAST_STATUS,
)
from ..services import runtime
from .core import ReconIssue, ReconResult, reconcile_once
from .reconciler import Reconciler

LOGGER = logging.getLogger(__name__)

_CRITICAL_HOLD_CODES = {"POSITION_MISMATCH", "BALANCE_MISMATCH", "ORDER_DESYNC"}
_STATUS_ORDER = {"OK": 0, "WARN": 1, "CRITICAL": 2}


@dataclass(slots=True)
class DaemonConfig:
    enabled: bool = True
    interval_sec: float = 20.0
    epsilon_position: Decimal = Decimal("0.0001")
    epsilon_balance: Decimal = Decimal("0.5")
    epsilon_notional: Decimal = Decimal("5.0")
    auto_hold_on_critical: bool = True


class ReconDaemon:
    """Manage periodic reconciliation sweeps."""

    def __init__(self, config: DaemonConfig | None = None) -> None:
        self._config = config or _resolve_daemon_config()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if not self._config.enabled:
            LOGGER.info("recon.daemon_disabled")
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:  # pragma: no cover - lifecycle cleanup
            LOGGER.debug("recon.daemon_loop_cancelled")
        finally:
            self._task = None

    async def run_once(self) -> ReconResult:
        state = runtime.get_state()
        recon_context = await self._build_context(state)
        result = reconcile_once(recon_context)
        self._handle_result(result)
        return result

    async def _run_loop(self) -> None:
        LOGGER.info("recon.daemon_start", extra={"interval": self._config.interval_sec})
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                await self.run_once()
            except Exception:  # pragma: no cover - defensive logging
                LOGGER.exception("recon.daemon_cycle_failed")
            elapsed = time.perf_counter() - started
            delay = max(self._config.interval_sec - elapsed, 0.5)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue
        LOGGER.info("recon.daemon_stop")

    async def _build_context(self, state) -> SimpleNamespace:
        local_positions, local_balances, local_orders = await asyncio.gather(
            self._fetch_local_positions(),
            self._fetch_local_balances(),
            self._fetch_local_orders(),
        )
        remote_positions, remote_balances, remote_orders = await asyncio.gather(
            self._fetch_remote_positions(),
            self._fetch_remote_balances(state),
            self._fetch_remote_orders(state),
        )

        cfg = SimpleNamespace(
            recon=SimpleNamespace(
                epsilon_position=self._config.epsilon_position,
                epsilon_balance=self._config.epsilon_balance,
                epsilon_notional=self._config.epsilon_notional,
                auto_hold_on_critical=self._config.auto_hold_on_critical,
            )
        )

        return SimpleNamespace(
            cfg=cfg,
            local_positions=lambda: local_positions,
            remote_positions=lambda: remote_positions,
            local_balances=lambda: local_balances,
            remote_balances=lambda: remote_balances,
            local_orders=lambda: local_orders,
            remote_orders=lambda: remote_orders,
        )

    async def _fetch_local_positions(self) -> Sequence[Mapping[str, object]]:
        try:
            return await asyncio.to_thread(ledger.fetch_positions)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "recon.local_positions_failed",
                extra={"event": "recon_error", "error": str(exc)},
            )
            return []

    async def _fetch_local_balances(self) -> Sequence[Mapping[str, object]]:
        try:
            return await asyncio.to_thread(ledger.fetch_balances)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "recon.local_balances_failed",
                extra={"event": "recon_error", "error": str(exc)},
            )
            return []

    async def _fetch_local_orders(self) -> Sequence[Mapping[str, object]]:
        try:
            return await asyncio.to_thread(ledger.fetch_open_orders)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "recon.local_orders_failed",
                extra={"event": "recon_error", "error": str(exc)},
            )
            return []

    async def _fetch_remote_positions(self) -> Mapping[tuple[str, str], object]:
        reconciler = Reconciler()
        try:
            return await asyncio.to_thread(reconciler.fetch_exchange_positions)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "recon.remote_positions_failed",
                extra={"event": "recon_error", "error": str(exc)},
            )
            return {}

    async def _fetch_remote_balances(self, state) -> Sequence[Mapping[str, object]]:
        runtime_deriv = getattr(state, "derivatives", None)
        venues = getattr(runtime_deriv, "venues", {}) if runtime_deriv else {}
        if not venues:
            return []
        router = ExecutionRouter()
        tasks = []
        venue_order: list[str] = []
        for venue_id in venues.keys():
            venue = venue_id.replace("_", "-")
            broker = router.broker_for_venue(venue)
            tasks.append(self._broker_balances(broker, venue))
            venue_order.append(venue)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        balances: list[Mapping[str, object]] = []
        for venue, result in zip(venue_order, results):
            if isinstance(result, Exception):
                LOGGER.warning(
                    "recon.remote_balances_error",
                    extra={"event": "recon_error", "venue": venue, "error": str(result)},
                )
                continue
            balances.extend(result)
        return balances

    async def _broker_balances(self, broker, venue: str) -> list[Mapping[str, object]]:
        try:
            response = await asyncio.wait_for(broker.balances(venue=venue), timeout=5.0)
        except asyncio.TimeoutError:
            LOGGER.warning(
                "recon.remote_balances_timeout",
                extra={"event": "recon_error", "venue": venue, "category": "balances"},
            )
            return []
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "recon.remote_balances_failed",
                extra={"event": "recon_error", "venue": venue, "error": str(exc)},
            )
            return []
        payload = response.get("balances") if isinstance(response, Mapping) else None
        if not isinstance(payload, Sequence):
            return []
        return [row for row in payload if isinstance(row, Mapping)]

    async def _fetch_remote_orders(self, state) -> Sequence[Mapping[str, object]]:
        runtime_deriv = getattr(state, "derivatives", None)
        venues = getattr(runtime_deriv, "venues", {}) if runtime_deriv else {}
        if not venues:
            return []
        tasks: list[asyncio.Task[list[Mapping[str, object]]]] = []
        venue_order: list[str] = []
        for venue_id, venue_state in venues.items():
            client = getattr(venue_state, "client", None)
            if client is None:
                continue
            venue = venue_id.replace("_", "-")
            tasks.append(asyncio.create_task(self._client_orders(client, venue)))
            venue_order.append(venue)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        orders: list[Mapping[str, object]] = []
        for venue, result in zip(venue_order, results):
            if isinstance(result, Exception):
                LOGGER.warning(
                    "recon.remote_orders_error",
                    extra={"event": "recon_error", "venue": venue, "error": str(result)},
                )
                continue
            orders.extend(result)
        return orders

    async def _client_orders(self, client, venue: str) -> list[Mapping[str, object]]:
        try:
            payload = await asyncio.wait_for(asyncio.to_thread(client.open_orders), timeout=5.0)
        except asyncio.TimeoutError:
            LOGGER.warning(
                "recon.remote_orders_timeout",
                extra={"event": "recon_error", "venue": venue, "category": "orders"},
            )
            return []
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "recon.remote_orders_failed",
                extra={"event": "recon_error", "venue": venue, "error": str(exc)},
            )
            return []
        if not isinstance(payload, Sequence):
            return []
        return [dict(row) for row in payload if isinstance(row, Mapping)]

    def _handle_result(self, result: ReconResult) -> None:
        worst = _classify_worst(result.issues)
        auto_hold = False
        hold_issue: ReconIssue | None = None
        for issue in result.issues:
            self._log_issue(issue)
            RECON_ISSUES_TOTAL.labels(
                kind=issue.kind,
                code=issue.code,
                severity=issue.severity,
            ).inc()
            if (
                not auto_hold
                and issue.severity == "CRITICAL"
                and issue.code in _CRITICAL_HOLD_CODES
                and self._config.auto_hold_on_critical
            ):
                auto_hold = True
                hold_issue = issue

        if auto_hold and hold_issue is not None:
            reason = f"RECON_CRITICAL::{hold_issue.code}"
            engaged = runtime.engage_safety_hold(reason, source="recon")
            if engaged:
                RECON_AUTO_HOLD_COUNTER.inc()
                LOGGER.error(
                    "recon.auto_hold_engaged",
                    extra={
                        "event": "recon_issue",
                        "severity": "CRITICAL",
                        "code": hold_issue.code,
                        "venue": hold_issue.venue,
                        "symbol": hold_issue.symbol,
                    },
                )

        RECON_LAST_RUN_TS.set(result.ts)
        for status in ("OK", "WARN", "CRITICAL"):
            value = 1.0 if status == worst else 0.0
            RECON_LAST_STATUS.labels(status=status).set(value)

        issues_payload = [
            {
                "kind": issue.kind,
                "code": issue.code,
                "severity": issue.severity,
                "venue": issue.venue,
                "symbol": issue.symbol,
                "details": issue.details,
            }
            for issue in result.issues
        ]
        runtime.update_reconciliation_status(
            issues=issues_payload,
            diffs=[],
            metadata={
                "status": worst,
                "state": worst,
                "auto_hold": auto_hold,
                "last_run_ts": result.ts,
                "issues_last_sample": issues_payload,
            },
        )

    def _log_issue(self, issue: ReconIssue) -> None:
        level = logging.INFO
        if issue.severity == "WARN":
            level = logging.WARNING
        elif issue.severity == "CRITICAL":
            level = logging.ERROR
        LOGGER.log(
            level,
            "recon.issue",
            extra={
                "event": "recon_issue",
                "severity": issue.severity,
                "kind": issue.kind,
                "code": issue.code,
                "venue": issue.venue,
                "symbol": issue.symbol,
                "details": issue.details,
            },
        )


def _classify_worst(issues: Sequence[ReconIssue]) -> str:
    worst = "OK"
    for issue in issues:
        if _STATUS_ORDER.get(issue.severity, 0) > _STATUS_ORDER[worst]:
            worst = issue.severity
            if worst == "CRITICAL":
                break
    return worst


def _resolve_daemon_config(cfg: object | None = None) -> DaemonConfig:
    state = runtime.get_state()
    source = getattr(getattr(state, "config", None), "data", None)
    if cfg is not None:
        candidate = getattr(cfg, "recon", None)
        if candidate is not None:
            source = candidate
    recon_cfg = None
    if isinstance(source, Mapping):
        recon_cfg = source.get("recon")
    else:
        recon_cfg = getattr(source, "recon", None)
    if recon_cfg is None:
        recon_cfg = getattr(cfg, "recon", None) if cfg is not None else None
    config = DaemonConfig()
    if recon_cfg is None:
        return config

    def _cfg_value(container: object, name: str) -> object | None:
        if isinstance(container, Mapping):
            return container.get(name)
        return getattr(container, name, None)

    def _extract(name: str, default: Decimal) -> Decimal:
        value = _cfg_value(recon_cfg, name)
        if value is None:
            return default
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except Exception:
            return default

    enabled_raw = _cfg_value(recon_cfg, "enabled")
    interval_raw = _cfg_value(recon_cfg, "interval_sec")
    auto_hold_raw = _cfg_value(recon_cfg, "auto_hold_on_critical")

    return DaemonConfig(
        enabled=bool(enabled_raw) if enabled_raw is not None else config.enabled,
        interval_sec=float(interval_raw) if interval_raw not in (None, "") else config.interval_sec,
        epsilon_position=_extract("epsilon_position", config.epsilon_position),
        epsilon_balance=_extract("epsilon_balance", config.epsilon_balance),
        epsilon_notional=_extract("epsilon_notional", config.epsilon_notional),
        auto_hold_on_critical=bool(auto_hold_raw)
        if auto_hold_raw is not None
        else config.auto_hold_on_critical,
    )


async def run_recon_cycle(*, config: DaemonConfig | None = None) -> ReconResult:
    daemon = ReconDaemon(config)
    return await daemon.run_once()


async def run_recon_loop(interval: float | None = None) -> None:
    config = _resolve_daemon_config()
    if interval is not None:
        config.interval_sec = interval
    daemon = ReconDaemon(config)
    await daemon.start()
    if daemon._task is not None:
        await daemon._task


def start_recon_daemon(ctx: object | None, cfg: object | None) -> ReconDaemon:
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
