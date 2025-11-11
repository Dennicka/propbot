"""Periodic reconciliation daemon orchestrating state comparisons."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Mapping, Sequence

from .. import ledger
from ..broker.router import ExecutionRouter
from ..metrics.recon import (
    RECON_AUTO_HOLD_COUNTER,
    RECON_DRIFT_TOTAL,
    RECON_ISSUES_TOTAL,
    RECON_LAST_RUN_TS,
    RECON_LAST_SEVERITY,
    RECON_LAST_STATUS,
)
from ..services import runtime
from .core import (
    ReconDrift,
    ReconIssue,
    ReconResult,
    detect_balance_drifts,
    detect_order_drifts,
    detect_position_drifts,
)
from .reconciler import Reconciler

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DaemonConfig:
    enabled: bool = True
    interval_sec: float = 20.0
    epsilon_position: Decimal = Decimal("0.0001")
    epsilon_balance: Decimal = Decimal("0.5")
    epsilon_notional: Decimal = Decimal("5.0")
    auto_hold_on_critical: bool = True
    balance_warn_usd: Decimal = Decimal("10")
    balance_critical_usd: Decimal = Decimal("100")
    position_size_warn: Decimal = Decimal("0.001")
    position_size_critical: Decimal = Decimal("0.01")
    order_critical_missing: bool = True


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
        drifts = run_recon_cycle(recon_context)
        return _drifts_to_result(drifts)

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
                balance_warn_usd=self._config.balance_warn_usd,
                balance_critical_usd=self._config.balance_critical_usd,
                position_size_warn=self._config.position_size_warn,
                position_size_critical=self._config.position_size_critical,
                order_critical_missing=self._config.order_critical_missing,
                enabled=self._config.enabled,
            )
        )

        return SimpleNamespace(
            cfg=cfg,
            state=state,
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


def _ctx_fetch(ctx, name: str, default):
    if ctx is None:
        return default() if callable(default) else default
    provider = getattr(ctx, name, None)
    if callable(provider):
        try:
            return provider()
        except Exception as exc:
            LOGGER.exception(
                "recon.ctx_provider_failed",
                extra={"provider": name, "error": str(exc)},
            )
            return default() if callable(default) else default
    if provider is not None:
        return provider
    return default() if callable(default) else default


def _ctx_recon_config(ctx) -> object:
    if ctx is None:
        return None
    candidate = getattr(ctx, "cfg", None)
    if candidate is None:
        return getattr(ctx, "recon", None)
    recon_cfg = getattr(candidate, "recon", None)
    if recon_cfg is not None:
        return recon_cfg
    return candidate


def _auto_hold_enabled(recon_cfg: object | None) -> bool:
    return bool(getattr(recon_cfg, "auto_hold_on_critical", True))


def _engage_hold(ctx, drifts: Sequence[ReconDrift]) -> bool:
    state = getattr(ctx, "state", None)
    safety = getattr(state, "safety", None)
    if safety is not None and getattr(safety, "hold_active", False):
        return False
    critical = next((drift for drift in drifts if drift.severity == "CRITICAL"), None)
    engaged = runtime.engage_safety_hold("RECON_DIVERGENCE", source="recon")
    if engaged:
        RECON_AUTO_HOLD_COUNTER.inc()
        LOGGER.error(
            "recon.auto_hold_engaged",
            extra={
                "event": "recon_drift",
                "severity": "CRITICAL",
                "kind": critical.kind if critical else None,
                "venue": critical.venue if critical else None,
                "symbol": critical.symbol if critical else None,
            },
        )
    return engaged


def _worst_severity(drifts: Sequence[ReconDrift]) -> str:
    worst = "OK"
    for drift in drifts:
        severity = str(drift.severity or "").upper()
        if severity == "CRITICAL":
            return "CRITICAL"
        if severity == "WARN" and worst != "CRITICAL":
            worst = "WARN"
    return worst


def _update_metrics(ts: float, worst: str) -> None:
    RECON_LAST_RUN_TS.set(ts)
    for status in ("OK", "WARN", "CRITICAL"):
        value = 1.0 if status == worst else 0.0
        RECON_LAST_STATUS.labels(status=status).set(value)
    severity_map = {"OK": 0.0, "WARN": 1.0, "CRITICAL": 2.0}
    RECON_LAST_SEVERITY.set(severity_map.get(worst, 0.0))


def _log_drift(drift: ReconDrift) -> None:
    level = logging.INFO
    if drift.severity == "WARN":
        level = logging.WARNING
    elif drift.severity == "CRITICAL":
        level = logging.ERROR
    LOGGER.log(
        level,
        "recon.drift",
        extra={
            "event": "recon_drift",
            "severity": drift.severity,
            "kind": drift.kind,
            "venue": drift.venue,
            "symbol": drift.symbol,
            "delta": drift.delta,
        },
    )


def _drift_code(drift: ReconDrift) -> str:
    return {
        "BALANCE": "BALANCE_DRIFT",
        "POSITION": "POSITION_DRIFT",
        "ORDER": "ORDER_DRIFT",
    }.get(drift.kind, f"{drift.kind}_DRIFT")


def _update_runtime_snapshot(
    drifts: Sequence[ReconDrift],
    worst: str,
    ts: float,
    recon_cfg: object | None,
    hold_engaged: bool,
) -> None:
    payload = [_drift_payload(drift) for drift in drifts]
    metadata = {
        "status": worst,
        "state": worst,
        "auto_hold": hold_engaged,
        "last_run_ts": ts,
        "issues_last_sample": payload,
        "drifts": payload,
        "drift_count": len(payload),
        "last_severity": worst,
        "enabled": bool(getattr(recon_cfg, "enabled", True)),
    }
    runtime.update_reconciliation_status(
        issues=payload,
        diffs=[],
        desync_detected=worst != "OK",
        metadata=metadata,
    )


def _drift_payload(drift: ReconDrift) -> dict[str, object]:
    return {
        "kind": drift.kind,
        "venue": drift.venue,
        "symbol": drift.symbol,
        "severity": drift.severity,
        "local": drift.local,
        "remote": drift.remote,
        "delta": drift.delta,
        "ts": drift.ts,
    }


def _drift_to_issue(drift: ReconDrift) -> ReconIssue:
    return ReconIssue(
        kind=drift.kind,
        venue=drift.venue,
        symbol=drift.symbol or "",
        severity=drift.severity,
        code=_drift_code(drift),
        details=_describe_drift(drift),
    )


def _drifts_to_result(drifts: Sequence[ReconDrift]) -> ReconResult:
    ts = drifts[0].ts if drifts else time.time()
    issues = [_drift_to_issue(drift) for drift in drifts]
    return ReconResult(ts=ts, issues=issues)


def _describe_drift(drift: ReconDrift) -> str:
    return f"local={drift.local} remote={drift.remote} delta={drift.delta}"


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
        except (InvalidOperation, TypeError, ValueError):
            return default

    enabled_raw = _cfg_value(recon_cfg, "enabled")
    interval_raw = _cfg_value(recon_cfg, "interval_sec")
    if interval_raw in (None, ""):
        interval_raw = _cfg_value(recon_cfg, "interval_s")
    auto_hold_raw = _cfg_value(recon_cfg, "auto_hold_on_critical")
    balance_warn = _extract("balance_warn_usd", config.balance_warn_usd)
    balance_critical = _extract("balance_critical_usd", config.balance_critical_usd)
    position_warn = _extract("position_size_warn", config.position_size_warn)
    position_critical = _extract("position_size_critical", config.position_size_critical)
    order_missing_raw = _cfg_value(recon_cfg, "order_critical_missing")

    return DaemonConfig(
        enabled=bool(enabled_raw) if enabled_raw is not None else config.enabled,
        interval_sec=float(interval_raw) if interval_raw not in (None, "") else config.interval_sec,
        epsilon_position=_extract("epsilon_position", config.epsilon_position),
        epsilon_balance=_extract("epsilon_balance", config.epsilon_balance),
        epsilon_notional=_extract("epsilon_notional", config.epsilon_notional),
        auto_hold_on_critical=(
            bool(auto_hold_raw) if auto_hold_raw is not None else config.auto_hold_on_critical
        ),
        balance_warn_usd=balance_warn,
        balance_critical_usd=balance_critical,
        position_size_warn=position_warn,
        position_size_critical=position_critical,
        order_critical_missing=(
            bool(order_missing_raw)
            if order_missing_raw is not None
            else config.order_critical_missing
        ),
    )


def run_recon_cycle(ctx) -> list[ReconDrift]:
    """Execute a single reconciliation pass using the provided context."""

    recon_cfg = _ctx_recon_config(ctx)
    local_balances = _ctx_fetch(ctx, "local_balances", lambda: [])
    remote_balances = _ctx_fetch(ctx, "remote_balances", lambda: [])
    local_positions = _ctx_fetch(ctx, "local_positions", lambda: [])
    remote_positions = _ctx_fetch(ctx, "remote_positions", lambda: [])
    local_orders = _ctx_fetch(ctx, "local_orders", lambda: [])
    remote_orders = _ctx_fetch(ctx, "remote_orders", lambda: [])

    drifts: list[ReconDrift] = []
    drifts.extend(detect_balance_drifts(local_balances, remote_balances, recon_cfg))
    drifts.extend(detect_position_drifts(local_positions, remote_positions, recon_cfg))
    drifts.extend(detect_order_drifts(local_orders, remote_orders, recon_cfg))

    worst = _worst_severity(drifts)
    ts = drifts[0].ts if drifts else time.time()

    hold_engaged = False
    for drift in drifts:
        _log_drift(drift)
        RECON_DRIFT_TOTAL.labels(kind=drift.kind, severity=drift.severity).inc()
        RECON_ISSUES_TOTAL.labels(
            kind=drift.kind,
            code=_drift_code(drift),
            severity=drift.severity,
        ).inc()
    if worst == "CRITICAL" and _auto_hold_enabled(recon_cfg):
        hold_engaged = _engage_hold(ctx, drifts)

    _update_metrics(ts, worst)
    _update_runtime_snapshot(drifts, worst, ts, recon_cfg, hold_engaged)
    return drifts


async def run_recon_cycle_async(*, config: DaemonConfig | None = None) -> ReconResult:
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
    "run_recon_cycle_async",
    "start_recon_daemon",
]
