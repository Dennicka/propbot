"""Periodic reconciliation daemon orchestrating state comparisons."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Iterable, Mapping, Sequence

import httpx

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
    ReconConfig,
    ReconReport,
    detect_balance_drifts,
    detect_order_drifts,
    detect_pnl_drifts,
    detect_position_drifts,
)
from .core import _ledger_rows_from_pnl, Reconciler as StalenessReconciler
from .reconciler import Reconciler

LOGGER = logging.getLogger(__name__)
_DEFAULT_GC_TTL_SEC = 300


_LEDGER_ERRORS: tuple[type[Exception], ...] = (
    sqlite3.Error,
    RuntimeError,
    OSError,
    ValueError,
)
_REMOTE_ERRORS: tuple[type[Exception], ...] = (
    httpx.HTTPError,
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    LookupError,
    OSError,
)
_PNL_ERRORS: tuple[type[Exception], ...] = _LEDGER_ERRORS + (
    InvalidOperation,
    TypeError,
    LookupError,
)
_PROVIDER_ERRORS: tuple[type[Exception], ...] = _REMOTE_ERRORS + (sqlite3.Error,)


def _log_recon_failure(
    message: str, *, level: int, exc: BaseException, details: Mapping[str, object]
) -> None:
    LOGGER.log(
        level,
        message,
        extra={
            "event": message,
            "component": "recon",
            "details": dict(details),
        },
        exc_info=exc,
    )


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
    pnl_warn_usd: Decimal = Decimal("10")
    pnl_critical_usd: Decimal = Decimal("50")
    pnl_relative_warn: Decimal = Decimal("0.02")
    pnl_relative_critical: Decimal = Decimal("0.05")
    fee_warn_usd: Decimal = Decimal("5")
    fee_critical_usd: Decimal = Decimal("20")
    funding_warn_usd: Decimal = Decimal("5")
    funding_critical_usd: Decimal = Decimal("25")


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
            except _REMOTE_ERRORS + _PNL_ERRORS as exc:  # pragma: no cover - defensive logging
                _log_recon_failure(
                    "recon.daemon_cycle_failed",
                    level=logging.ERROR,
                    exc=exc,
                    details={"stage": "run_once"},
                )
            elapsed = time.perf_counter() - started
            delay = max(self._config.interval_sec - elapsed, 0.5)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue
        LOGGER.info("recon.daemon_stop")

    async def _build_context(self, state) -> SimpleNamespace:
        local_positions, local_balances, local_orders, local_pnl = await asyncio.gather(
            self._fetch_local_positions(),
            self._fetch_local_balances(),
            self._fetch_local_orders(),
            self._fetch_local_pnl(),
        )
        remote_positions, remote_balances, remote_orders, remote_pnl = await asyncio.gather(
            self._fetch_remote_positions(),
            self._fetch_remote_balances(state),
            self._fetch_remote_orders(state),
            self._fetch_remote_pnl(state),
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
                pnl_warn_usd=self._config.pnl_warn_usd,
                pnl_critical_usd=self._config.pnl_critical_usd,
                pnl_relative_warn=self._config.pnl_relative_warn,
                pnl_relative_critical=self._config.pnl_relative_critical,
                fee_warn_usd=self._config.fee_warn_usd,
                fee_critical_usd=self._config.fee_critical_usd,
                funding_warn_usd=self._config.funding_warn_usd,
                funding_critical_usd=self._config.funding_critical_usd,
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
            local_pnl=lambda: local_pnl,
            remote_pnl=lambda: remote_pnl,
        )

    async def _fetch_local_positions(self) -> Sequence[Mapping[str, object]]:
        try:
            return await asyncio.to_thread(ledger.fetch_positions)
        except _LEDGER_ERRORS as exc:  # pragma: no cover - defensive
            _log_recon_failure(
                "recon.local_positions_failed",
                level=logging.ERROR,
                exc=exc,
                details={"source": "ledger.fetch_positions"},
            )
            return []

    async def _fetch_local_balances(self) -> Sequence[Mapping[str, object]]:
        try:
            return await asyncio.to_thread(ledger.fetch_balances)
        except _LEDGER_ERRORS as exc:  # pragma: no cover - defensive
            _log_recon_failure(
                "recon.local_balances_failed",
                level=logging.ERROR,
                exc=exc,
                details={"source": "ledger.fetch_balances"},
            )
            return []

    async def _fetch_local_orders(self) -> Sequence[Mapping[str, object]]:
        try:
            return await asyncio.to_thread(ledger.fetch_open_orders)
        except _LEDGER_ERRORS as exc:  # pragma: no cover - defensive
            _log_recon_failure(
                "recon.local_orders_failed",
                level=logging.ERROR,
                exc=exc,
                details={"source": "ledger.fetch_open_orders"},
            )
            return []

    async def _fetch_local_pnl(self) -> Sequence[Mapping[str, object]]:
        since_ts = await asyncio.to_thread(_determine_pnl_since_ts)
        try:
            return await asyncio.to_thread(_build_local_pnl_snapshot, since_ts)
        except _PNL_ERRORS as exc:  # pragma: no cover - defensive
            _log_recon_failure(
                "recon.local_pnl_failed",
                level=logging.ERROR,
                exc=exc,
                details={"source": "pnl_snapshot", "since": since_ts},
            )
            return []

    async def _fetch_remote_positions(self) -> Mapping[tuple[str, str], object]:
        reconciler = Reconciler()
        try:
            return await asyncio.to_thread(reconciler.fetch_exchange_positions)
        except _REMOTE_ERRORS as exc:  # pragma: no cover - defensive
            _log_recon_failure(
                "recon.remote_positions_failed",
                level=logging.WARNING,
                exc=exc,
                details={"source": "exchange.positions"},
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
                    extra={
                        "event": "recon.remote_balances_error",
                        "component": "recon",
                        "details": {"venue": venue, "error": str(result)},
                    },
                    exc_info=result,
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
                extra={
                    "event": "recon.remote_balances_timeout",
                    "component": "recon",
                    "details": {"venue": venue, "category": "balances"},
                },
            )
            return []
        except _REMOTE_ERRORS as exc:  # pragma: no cover - defensive
            _log_recon_failure(
                "recon.remote_balances_failed",
                level=logging.WARNING,
                exc=exc,
                details={"venue": venue, "category": "balances"},
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
                    extra={
                        "event": "recon.remote_orders_error",
                        "component": "recon",
                        "details": {"venue": venue, "error": str(result)},
                    },
                    exc_info=result,
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
                extra={
                    "event": "recon.remote_orders_timeout",
                    "component": "recon",
                    "details": {"venue": venue, "category": "orders"},
                },
            )
            return []
        except _REMOTE_ERRORS as exc:  # pragma: no cover - defensive
            _log_recon_failure(
                "recon.remote_orders_failed",
                level=logging.WARNING,
                exc=exc,
                details={"venue": venue, "category": "orders"},
            )
            return []
        if not isinstance(payload, Sequence):
            return []
        return [dict(row) for row in payload if isinstance(row, Mapping)]

    async def _fetch_remote_pnl(self, state) -> Sequence[Mapping[str, object]]:
        runtime_deriv = getattr(state, "derivatives", None)
        venues = getattr(runtime_deriv, "venues", {}) if runtime_deriv else {}
        if not venues:
            return []
        router = ExecutionRouter()
        since_ts = await asyncio.to_thread(_determine_pnl_since_ts)
        since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc) if since_ts else None
        tasks: list[asyncio.Task[list[Mapping[str, object]]]] = []
        venue_order: list[str] = []
        for venue_id in venues.keys():
            venue = venue_id.replace("_", "-")
            broker = router.broker_for_venue(venue)
            if broker is None:
                continue
            venue_order.append(venue)
            tasks.append(asyncio.create_task(self._broker_pnl(broker, venue, since_dt, since_ts)))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        snapshots: list[Mapping[str, object]] = []
        for venue, result in zip(venue_order, results):
            if isinstance(result, Exception):
                LOGGER.warning(
                    "recon.remote_pnl_failed",
                    extra={
                        "event": "recon.remote_pnl_failed",
                        "component": "recon",
                        "details": {"venue": venue, "error": str(result)},
                    },
                    exc_info=result,
                )
                continue
            snapshots.extend(result)
        return snapshots

    async def _broker_pnl(
        self,
        broker,
        venue: str,
        since_dt: datetime | None,
        since_ts: float | None,
    ) -> list[Mapping[str, object]]:
        try:
            fills_payload = await asyncio.wait_for(broker.get_fills(since=since_dt), timeout=10.0)
        except asyncio.TimeoutError:
            LOGGER.warning(
                "recon.remote_pnl_timeout",
                extra={
                    "event": "recon.remote_pnl_timeout",
                    "component": "recon",
                    "details": {"venue": venue, "category": "fills"},
                },
            )
            return []
        except _REMOTE_ERRORS as exc:  # pragma: no cover - defensive
            _log_recon_failure(
                "recon.remote_pnl_fetch_failed",
                level=logging.WARNING,
                exc=exc,
                details={"venue": venue, "category": "fills"},
            )
            return []
        fills: list[Mapping[str, object]] = []
        if isinstance(fills_payload, Iterable):
            for item in fills_payload:
                normalised = _normalise_remote_fill(venue, item)
                if normalised is not None:
                    fills.append(normalised)
        supports_fees = any("fee" in row and row["fee"] not in (None, "") for row in fills)
        source = _StaticPnLSource(fills, [], supports_fees=supports_fees, supports_funding=False)
        try:
            return await asyncio.to_thread(
                _build_remote_pnl_snapshot,
                source,
                since_ts,
                supports_fees,
                source.supports_funding,
            )
        except _PNL_ERRORS as exc:  # pragma: no cover - defensive
            _log_recon_failure(
                "recon.remote_pnl_snapshot_failed",
                level=logging.WARNING,
                exc=exc,
                details={"venue": venue, "category": "pnl_snapshot"},
            )
            return []


def _ctx_fetch(ctx, name: str, default):
    if ctx is None:
        return default() if callable(default) else default
    provider = getattr(ctx, name, None)
    if callable(provider):
        try:
            return provider()
        except _PROVIDER_ERRORS as exc:  # defensive: recon must proceed even if provider fails
            _log_recon_failure(
                "recon.ctx_provider_failed",
                level=logging.WARNING,
                exc=exc,
                details={"provider": name},
            )
            return default() if callable(default) else default
    if provider is not None:
        return provider
    return default() if callable(default) else default


def _determine_pnl_since_ts() -> float | None:
    try:
        recent = ledger.fetch_recent_fills(200)
    except _LEDGER_ERRORS as exc:  # pragma: no cover - defensive
        _log_recon_failure(
            "recon.local_recent_fills_failed",
            level=logging.WARNING,
            exc=exc,
            details={"source": "ledger.fetch_recent_fills"},
        )
        return None
    timestamps: list[float] = []
    for row in recent:
        if not isinstance(row, Mapping):
            continue
        ts_value = _coerce_timestamp(row.get("ts"))
        if ts_value is not None:
            timestamps.append(ts_value)
    if not timestamps:
        return None
    earliest = min(timestamps)
    window = max(earliest - 3600, 0.0)
    return window


def _build_local_pnl_snapshot(since_ts: float | None) -> list[Mapping[str, object]]:
    from ..ledger import pnl_sources

    pnl_ledger = pnl_sources.build_ledger_from_history(None, since_ts=since_ts)
    return _ledger_rows_from_pnl(pnl_ledger, supports_fees=True, supports_funding=True)


def _build_remote_pnl_snapshot(
    source: "_StaticPnLSource",
    since_ts: float | None,
    supports_fees: bool,
    supports_funding: bool,
) -> list[Mapping[str, object]]:
    from ..ledger import pnl_sources

    pnl_ledger = pnl_sources.build_ledger_from_history(source, since_ts=since_ts)
    return _ledger_rows_from_pnl(
        pnl_ledger,
        supports_fees=supports_fees,
        supports_funding=supports_funding,
    )


def _coerce_timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            cleaned = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            try:
                parsed = datetime.fromisoformat(cleaned)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            return parsed.timestamp()
    return None


def _isoformat_ts(value: object) -> str:
    ts = _coerce_timestamp(value)
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _normalise_remote_fill(venue: str, payload: object) -> Mapping[str, object] | None:
    if not isinstance(payload, Mapping):
        return None
    symbol = str(
        payload.get("symbol") or payload.get("instId") or payload.get("instrument") or ""
    ).upper()
    if not symbol:
        return None
    qty = (
        payload.get("qty")
        or payload.get("size")
        or payload.get("quantity")
        or payload.get("base_qty")
    )
    price = payload.get("price") or payload.get("px") or payload.get("avgPrice")
    side = payload.get("side") or payload.get("direction")
    if side is None:
        buyer_flag = payload.get("buyer")
        if buyer_flag is True:
            side = "buy"
        elif buyer_flag is False:
            side = "sell"
    fee = payload.get("fee") or payload.get("commission") or payload.get("feePaid")
    fee_asset = (
        payload.get("fee_asset") or payload.get("feeCurrency") or payload.get("commissionAsset")
    )
    ts_value = payload.get("ts") or payload.get("timestamp") or payload.get("time")
    iso_ts = _isoformat_ts(ts_value)
    return {
        "venue": venue,
        "symbol": symbol,
        "qty": qty,
        "price": price,
        "side": side or "buy",
        "fee": fee,
        "fee_asset": fee_asset,
        "ts": iso_ts,
    }


class _StaticPnLSource:
    def __init__(
        self,
        fills: Sequence[Mapping[str, object]],
        events: Sequence[Mapping[str, object]],
        *,
        supports_fees: bool,
        supports_funding: bool,
    ) -> None:
        self._fills = [dict(row) for row in fills if isinstance(row, Mapping)]
        self._events = [dict(row) for row in events if isinstance(row, Mapping)]
        self.supports_fees = supports_fees
        self.supports_funding = supports_funding

    def fetch_fills_since(self, since: object | None = None) -> list[Mapping[str, object]]:
        threshold = None
        if since is not None:
            if isinstance(since, str):
                threshold = _coerce_timestamp(since)
            elif isinstance(since, (int, float)) and not isinstance(since, bool):
                threshold = float(since)
        if threshold is None:
            return list(self._fills)
        filtered: list[Mapping[str, object]] = []
        for row in self._fills:
            ts_value = _coerce_timestamp(row.get("ts"))
            if ts_value is None or ts_value < threshold:
                continue
            filtered.append(row)
        return filtered

    def fetch_events(self, **_) -> list[Mapping[str, object]]:
        return list(self._events)


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
        "PNL": "PNL_DRIFT",
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
    pnl_warn = _extract("pnl_warn_usd", config.pnl_warn_usd)
    pnl_critical = _extract("pnl_critical_usd", config.pnl_critical_usd)
    pnl_rel_warn = _extract("pnl_relative_warn", config.pnl_relative_warn)
    pnl_rel_critical = _extract("pnl_relative_critical", config.pnl_relative_critical)
    fee_warn = _extract("fee_warn_usd", config.fee_warn_usd)
    fee_critical = _extract("fee_critical_usd", config.fee_critical_usd)
    funding_warn = _extract("funding_warn_usd", config.funding_warn_usd)
    funding_critical = _extract("funding_critical_usd", config.funding_critical_usd)

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
        pnl_warn_usd=pnl_warn,
        pnl_critical_usd=pnl_critical,
        pnl_relative_warn=pnl_rel_warn,
        pnl_relative_critical=pnl_rel_critical,
        fee_warn_usd=fee_warn,
        fee_critical_usd=fee_critical,
        funding_warn_usd=funding_warn,
        funding_critical_usd=funding_critical,
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
    local_pnl = _ctx_fetch(ctx, "local_pnl", lambda: [])
    remote_pnl = _ctx_fetch(ctx, "remote_pnl", lambda: [])

    drifts: list[ReconDrift] = []
    drifts.extend(detect_balance_drifts(local_balances, remote_balances, recon_cfg))
    drifts.extend(detect_position_drifts(local_positions, remote_positions, recon_cfg))
    drifts.extend(detect_order_drifts(local_orders, remote_orders, recon_cfg))
    drifts.extend(detect_pnl_drifts(local_pnl, remote_pnl, recon_cfg))

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


class OrderReconDaemon:
    """Periodic daemon for lightweight order staleness reconciliation."""

    def __init__(self, reconciler: StalenessReconciler) -> None:
        self._reconciler = reconciler
        self._config: ReconConfig = reconciler.config
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_report: ReconReport | None = None

    async def start(self) -> None:
        """Start the reconciliation loop if not already running."""

        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the reconciliation loop and wait for completion."""

        if not self._task:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:  # pragma: no cover - lifecycle cleanup
            LOGGER.debug("recon_a.daemon_cancelled")
        finally:
            self._task = None

    async def get_last_report(self) -> ReconReport | None:
        """Return the last produced reconciliation report."""

        async with self._lock:
            return self._last_report

    async def _run_loop(self) -> None:
        interval = max(float(self._config.interval_sec), 0.1)
        LOGGER.info(
            "recon_a.daemon_start",
            extra={"interval": interval, "order_stale_sec": self._config.order_stale_sec},
        )
        try:
            while not self._stop_event.is_set():
                started = time.perf_counter()
                try:
                    report = self._reconciler.check_staleness(self._reconciler.now())
                except Exception:  # pragma: no cover - defensive
                    LOGGER.exception(
                        "recon_a.run_failed",
                        extra={"event": "recon_a_run_failed"},
                    )
                    report = None
                else:
                    await self._store_report(report)
                    self._log_report(report)
                    self._run_gc(report.ts)
                elapsed = time.perf_counter() - started
                delay = max(interval - elapsed, 0.0)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    continue
        finally:
            LOGGER.info("recon_a.daemon_stop")

    async def _store_report(self, report: ReconReport) -> None:
        async with self._lock:
            self._last_report = report

    @staticmethod
    def _log_report(report: ReconReport) -> None:
        LOGGER.info(
            "recon_a.summary",
            extra={"checked": report.checked, "issues": len(report.issues)},
        )
        for issue in report.issues:
            details = issue.details
            if isinstance(details, Mapping):
                details_payload = dict(details)
            elif details is None:
                details_payload = {}
            else:
                details_payload = {"details": details}
            LOGGER.warning(
                "recon_a.issue",
                extra={
                    "kind": issue.kind,
                    "order_id": issue.order_id,
                    "age_sec": issue.age_sec,
                    "details": details_payload,
                },
            )

    def _run_gc(self, now_ts: float) -> None:
        ttl_sec = int(getattr(self._config, "gc_ttl_sec", _DEFAULT_GC_TTL_SEC))
        if ttl_sec <= 0:
            return
        router = getattr(self._reconciler, "_router", None)
        if router is None:
            return
        purge_fn = getattr(router, "purge_terminal_orders", None)
        if purge_fn is None:
            return
        try:
            removed = purge_fn(ttl_sec=ttl_sec, now_ts=now_ts)
        except TypeError:
            removed = purge_fn(ttl_sec, now_ts)
        LOGGER.info("recon.gc_removed=%d", removed, extra={"removed": removed})


__all__ = [
    "ReconDaemon",
    "DaemonConfig",
    "OrderReconDaemon",
    "run_recon_loop",
    "run_recon_cycle",
    "run_recon_cycle_async",
    "start_recon_daemon",
]
