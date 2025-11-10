"""Background resolver that cancels and retries stuck orders."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Mapping, Sequence

from ..broker.router import ExecutionRouter
from ..metrics.execution import (
    OPEN_ORDERS_GAUGE,
    ORDER_RETRIES_TOTAL,
    STUCK_ORDERS_TOTAL,
    STUCK_RESOLVER_ACTIVE_INTENTS,
    STUCK_RESOLVER_FAILURES_TOTAL,
    STUCK_RESOLVER_RETRIES_TOTAL,
)
from ..persistence import order_store
from ..router.order_router import OrderRouter, OrderRouterError
from ..utils.identifiers import generate_request_id
from .. import ledger
from ..services import runtime


LOGGER = logging.getLogger(__name__)

_TERMINAL_LEDGER_STATUSES = {"filled", "cancelled", "canceled", "failed", "skipped"}
_PENDING_STATUSES = {
    "submitted",
    "open",
    "new",
    "ack",
    "acknowledged",
    "pending",
    "pending_cancel",
    "partially_filled",
}


def _normalise_symbol(value: object) -> str:
    text = str(value or "").strip()
    return text.upper() if text else "unknown"


def _normalise_venue(value: object) -> str:
    text = str(value or "").strip()
    return text.lower() if text else "unknown"


def _normalise_status(value: object) -> str:
    text = str(value or "").strip()
    return text.lower() if text else "unknown"


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _intent_set(order_rows: Iterable[Mapping[str, object]]) -> set[str]:
    intents: set[str] = set()
    for row in order_rows:
        if not isinstance(row, Mapping):
            continue
        intent = str(row.get("idemp_key") or "").strip()
        if intent:
            intents.add(intent)
    return intents


@dataclass
class _ResolverConfig:
    enabled: bool
    pending_timeout: float
    cancel_grace: float
    max_retries: int
    backoff: Sequence[float]


class _RouterBrokerAdapter:
    """Adapter that exposes ExecutionRouter brokers as a multi-venue broker."""

    def __init__(self, router: ExecutionRouter) -> None:
        self._router = router

    def _broker_for(self, venue: str):
        return self._router.broker_for_venue(venue)

    async def create_order(self, **params):  # type: ignore[override]
        venue = params.get("venue")
        broker = self._broker_for(str(venue or ""))
        result = broker.create_order(**params)
        return await result if asyncio.iscoroutine(result) else result

    async def cancel(self, **params):  # type: ignore[override]
        venue = params.get("venue")
        broker = self._broker_for(str(venue or ""))
        result = broker.cancel(**params)
        return await result if asyncio.iscoroutine(result) else result

    def supports_reduce_only(self, venue: str) -> bool:
        broker = self._broker_for(venue)
        support = getattr(broker, "supports_reduce_only", None)
        if isinstance(support, Mapping):
            key = venue.lower()
            return bool(support.get(key) or support.get(venue))
        if callable(support):
            try:
                return bool(support(venue=venue))
            except TypeError:
                try:
                    return bool(support(venue))
                except TypeError:
                    return bool(support())
        if isinstance(support, (set, frozenset, list, tuple)):
            return venue.lower() in {str(item).lower() for item in support}
        if isinstance(support, bool):
            return support
        native = getattr(broker, "native_reduce_only_venues", None)
        if isinstance(native, Mapping):
            return bool(native.get(venue) or native.get(venue.lower()))
        if isinstance(native, (set, frozenset, list, tuple)):
            return venue.lower() in {str(item).lower() for item in native}
        return bool(getattr(broker, "supports_reduce_only", False))


class StuckOrderResolver:
    """Detects stuck orders and retries them with exponential backoff."""

    def __init__(
        self,
        *,
        ctx=runtime,
        order_router: OrderRouter | None = None,
        execution_router: ExecutionRouter | None = None,
        poll_interval: float = 0.5,
    ) -> None:
        self._ctx = ctx
        self._ledger = ledger
        self._order_store = order_store
        self._poll_interval = max(float(poll_interval), 0.1)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_fill_poll: datetime | None = None
        self._fill_ts_by_order: Dict[int, datetime] = {}
        self._intent_fill_ack: Dict[str, datetime] = {}
        self._retry_counts: Dict[str, int] = {}
        self._backoff_until: Dict[str, float] = {}
        self._maxed_out: set[str] = set()
        self._open_order_labels: set[tuple[str, str, str]] = set()
        self._logger = LOGGER.getChild("resolver")
        self._execution_router = execution_router or ExecutionRouter()
        if order_router is None:
            broker_adapter = _RouterBrokerAdapter(self._execution_router)
            self._router = OrderRouter(broker_adapter)
        else:
            self._router = order_router

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def enabled(self) -> bool:
        config = self._resolver_config()
        return config.enabled

    def retries_last_hour(self) -> int:
        state = self._resolver_state()
        if state is None:
            return 0
        counter = getattr(state, "retries_last_hour", None)
        if callable(counter):
            try:
                return int(counter())
            except Exception:  # pragma: no cover - defensive
                return 0
        snapshot_getter = getattr(state, "snapshot", None)
        if callable(snapshot_getter):
            try:
                snapshot = snapshot_getter()
            except Exception:  # pragma: no cover - defensive
                snapshot = {}
            if isinstance(snapshot, Mapping):
                try:
                    return int(snapshot.get("retries_last_hour", 0))
                except (TypeError, ValueError):  # pragma: no cover - defensive
                    return 0
        return 0

    def get_status_badge(self) -> str:
        if not self.enabled:
            return ""
        retries = self.retries_last_hour()
        if retries < 0:
            retries = 0
        return f"ON (retries 1h: {retries})"

    async def start(self) -> None:
        config = self._resolver_config()
        if not config.enabled:
            self._logger.info("stuck resolver disabled by configuration")
            return
        if self.running:
            return
        self._logger.info(
            "stuck resolver starting",
            extra={"timeout": config.pending_timeout, "max_retries": config.max_retries},
        )
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
            self._logger.debug("stuck resolver task cancelled during stop")
        finally:
            self._task = None

    async def run_once(self) -> None:
        config = self._resolver_config()
        if not config.enabled:
            return
        await self._refresh_fills()
        orders = await asyncio.to_thread(self._ledger.fetch_open_orders)
        self._update_open_orders_gauge(orders)
        active_intents = _intent_set(orders)
        self._cleanup_stale(active_intents)
        STUCK_RESOLVER_ACTIVE_INTENTS.set(float(len(active_intents)))
        now = datetime.now(timezone.utc)
        now_ts = time.time()
        for order in orders:
            await self._process_order(order, config, now, now_ts)

    async def _run_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - defensive logging
                    self._logger.exception("stuck resolver iteration failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    continue
        finally:
            self._logger.info("stuck resolver stopped")

    def _resolver_state(self):
        getter = getattr(self._ctx, "get_state", None)
        if not callable(getter):
            return None
        state = getter()
        execution = getattr(state, "execution", None)
        return getattr(execution, "stuck_resolver", None)

    def _resolver_config(self) -> _ResolverConfig:
        state = self._resolver_state()
        if state is None:
            return _ResolverConfig(False, 0.0, 0.0, 0, [])
        enabled = bool(getattr(state, "enabled", False))
        timeout = max(_as_float(getattr(state, "pending_timeout_sec", 0.0)), 0.0)
        grace = max(_as_float(getattr(state, "cancel_grace_sec", 0.0)), 0.0)
        max_retries = int(getattr(state, "max_retries", 0) or 0)
        backoff_raw = getattr(state, "backoff_sec", []) or []
        if isinstance(backoff_raw, (list, tuple)):
            backoff = [
                max(_as_float(entry), 0.0) for entry in backoff_raw if _as_float(entry) >= 0.0
            ]
        else:
            backoff = []
        return _ResolverConfig(enabled, timeout, grace, max_retries, backoff or [0.0])

    async def _refresh_fills(self) -> None:
        since = self._last_fill_poll
        try:
            rows = await asyncio.to_thread(self._ledger.fetch_fills_since, since)
        except Exception:  # pragma: no cover - defensive logging
            self._logger.exception("failed to fetch fills for stuck resolver")
            return
        latest = since
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            ts = _parse_timestamp(row.get("ts"))
            order_id = row.get("order_id")
            try:
                order_key = int(order_id)
            except (TypeError, ValueError):
                continue
            if ts is None:
                continue
            self._fill_ts_by_order[order_key] = ts
            if latest is None or ts > latest:
                latest = ts
        if latest is None:
            latest = datetime.now(timezone.utc)
        self._last_fill_poll = latest

    def _update_open_orders_gauge(self, orders: Iterable[Mapping[str, object]]) -> None:
        counts: Counter[tuple[str, str, str]] = Counter()
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            venue = _normalise_venue(order.get("venue"))
            symbol = _normalise_symbol(order.get("symbol"))
            status = _normalise_status(order.get("status"))
            counts[(venue, symbol, status)] += 1
        for label in self._open_order_labels - set(counts.keys()):
            OPEN_ORDERS_GAUGE.labels(*label).set(0.0)
        for (venue, symbol, status), count in counts.items():
            OPEN_ORDERS_GAUGE.labels(venue, symbol, status).set(float(count))
        self._open_order_labels = set(counts.keys())

    def _cleanup_stale(self, intents: set[str]) -> None:
        stale = set(self._retry_counts) - intents
        for intent_id in stale:
            self._retry_counts.pop(intent_id, None)
            self._backoff_until.pop(intent_id, None)
            self._intent_fill_ack.pop(intent_id, None)
            self._maxed_out.discard(intent_id)
            self._ctx.clear_stuck_resolver_error(intent_id)

    async def _process_order(
        self,
        order: Mapping[str, object],
        config: _ResolverConfig,
        now: datetime,
        now_ts: float,
    ) -> None:
        if not isinstance(order, Mapping):
            return
        status = _normalise_status(order.get("status"))
        if status not in _PENDING_STATUSES:
            return
        intent_id = str(order.get("idemp_key") or "").strip()
        if not intent_id:
            return
        created_ts = _parse_timestamp(order.get("client_ts"))
        if created_ts is None:
            return
        age = (now - created_ts).total_seconds()
        if age <= config.pending_timeout:
            return
        retries = self._retry_counts.get(intent_id, 0)
        if config.max_retries > 0 and retries >= config.max_retries:
            self._mark_max_retries(intent_id)
            return
        backoff_until = self._backoff_until.get(intent_id, 0.0)
        if now_ts < backoff_until:
            return
        order_id_raw = order.get("id")
        try:
            order_id = int(order_id_raw)
        except (TypeError, ValueError):
            order_id = None
        if order_id is not None:
            fill_ts = self._fill_ts_by_order.get(order_id)
            if fill_ts is not None:
                last_seen = self._intent_fill_ack.get(intent_id)
                if last_seen is None or fill_ts > last_seen:
                    self._intent_fill_ack[intent_id] = fill_ts
                    self._retry_counts.pop(intent_id, None)
                    self._backoff_until.pop(intent_id, None)
                    self._maxed_out.discard(intent_id)
                    runtime.clear_stuck_resolver_error(intent_id)
                    return
        await self._retry_order(order, intent_id, retries, config, order_id, now_ts)

    def _mark_max_retries(self, intent_id: str) -> None:
        if intent_id in self._maxed_out:
            return
        self._maxed_out.add(intent_id)
        venue = "unknown"
        symbol = "unknown"
        request_id = None
        with self._order_store.session_scope() as session:
            intent = self._order_store.load_intent(session, intent_id)
            if intent is not None:
                venue = _normalise_venue(intent.venue)
                symbol = _normalise_symbol(intent.symbol)
                request_id = intent.request_id
        self._ctx.record_stuck_resolver_error(intent_id, "STUCK_MAX_RETRIES")
        self._ctx.record_incident(
            "ops_event",
            {
                "reason": "STUCK_MAX_RETRIES",
                "intent_id": intent_id,
                "venue": venue,
                "symbol": symbol,
                "request_id": request_id,
            },
        )
        STUCK_RESOLVER_FAILURES_TOTAL.labels(venue, symbol, "max_retries").inc()
        self._logger.warning(
            "stuck resolver max retries reached",
            extra={
                "intent_id": intent_id,
                "request_id": request_id,
                "venue": venue,
                "symbol": symbol,
                "reason": "STUCK_MAX_RETRIES",
            },
        )

    def _backoff_delay(self, retries: int, config: _ResolverConfig) -> float:
        if not config.backoff:
            return 0.0
        index = max(retries - 1, 0) % len(config.backoff)
        return max(config.backoff[index], 0.0)

    async def _retry_order(
        self,
        order: Mapping[str, object],
        intent_id: str,
        retries: int,
        config: _ResolverConfig,
        ledger_order_id: int | None,
        now_ts: float,
    ) -> None:
        with self._order_store.session_scope() as session:
            intent = self._order_store.load_intent(session, intent_id)
        if intent is None:
            return
        if intent.state in (
            order_store.OrderIntentState.FILLED,
            order_store.OrderIntentState.CANCELED,
            order_store.OrderIntentState.REJECTED,
            order_store.OrderIntentState.EXPIRED,
            order_store.OrderIntentState.REPLACED,
        ):
            return
        broker_order_id = intent.broker_order_id
        if not broker_order_id:
            return
        previous_request_id = intent.request_id
        self._ctx.clear_stuck_resolver_error(intent_id)
        venue = intent.venue
        symbol = intent.symbol
        STUCK_ORDERS_TOTAL.labels(_normalise_venue(venue), _normalise_symbol(symbol)).inc()
        try:
            await self._router.cancel_order(
                account=intent.account,
                venue=venue,
                broker_order_id=broker_order_id,
                request_id=f"cancel-{intent_id}-{retries+1}",
                reason="STUCK_TIMEOUT",
            )
        except OrderRouterError:
            self._logger.exception(
                "stuck resolver cancel failed",
                extra={
                    "event": "stuck_resolver_cancel_failed",
                    "intent_id": intent_id,
                    "request_id": previous_request_id,
                    "venue": _normalise_venue(venue),
                    "symbol": _normalise_symbol(symbol),
                    "broker_order_id": broker_order_id,
                },
            )
            return
        if config.cancel_grace > 0:
            try:
                await asyncio.sleep(config.cancel_grace)
            except asyncio.CancelledError:
                raise
        if ledger_order_id is not None:
            try:
                current = await asyncio.to_thread(self._ledger.get_order, ledger_order_id)
            except Exception:
                current = None
            status = (
                _normalise_status(current.get("status")) if isinstance(current, Mapping) else ""
            )
            if status in _TERMINAL_LEDGER_STATUSES:
                self._logger.info(
                    "stuck resolver cancel resolved order",
                    extra={
                        "intent_id": intent_id,
                        "ledger_order_id": ledger_order_id,
                        "status": status,
                    },
                )
                return
        new_request_id = generate_request_id()
        try:
            ref = await self._router.submit_order(
                account=intent.account,
                venue=venue,
                symbol=symbol,
                side=intent.side,
                order_type=intent.type,
                qty=float(intent.qty),
                price=float(intent.price) if intent.price is not None else None,
                tif=intent.tif,
                strategy=intent.strategy,
                intent_id=intent.intent_id,
                request_id=new_request_id,
            )
        except OrderRouterError:
            self._logger.exception(
                "stuck resolver retry failed",
                extra={
                    "event": "stuck_resolver_retry_failed",
                    "intent_id": intent_id,
                    "request_id": new_request_id,
                    "venue": _normalise_venue(venue),
                    "symbol": _normalise_symbol(symbol),
                },
            )
            return
        self._retry_counts[intent_id] = retries + 1
        delay = self._backoff_delay(retries + 1, config)
        self._backoff_until[intent_id] = now_ts + delay
        self._maxed_out.discard(intent_id)
        self._ctx.record_stuck_resolver_retry(intent_id=intent_id, timestamp=now_ts)
        ORDER_RETRIES_TOTAL.labels(_normalise_venue(venue), _normalise_symbol(symbol)).inc()
        STUCK_RESOLVER_RETRIES_TOTAL.labels(
            _normalise_venue(venue), _normalise_symbol(symbol), "timeout"
        ).inc()
        self._ctx.record_incident(
            "ops_event",
            {
                "reason": "STUCK_TIMEOUT",
                "intent_id": intent_id,
                "prev_order_id": ledger_order_id,
                "new_order_id": ref.broker_order_id,
                "retries_n": retries + 1,
                "venue": venue,
                "symbol": symbol,
                "previous_request_id": previous_request_id,
                "new_request_id": new_request_id,
            },
        )
        self._logger.info(
            "stuck order retried",
            extra={
                "intent_id": intent_id,
                "retries": retries + 1,
                "venue": venue,
                "symbol": symbol,
                "backoff_s": delay,
                "previous_request_id": previous_request_id,
                "new_request_id": new_request_id,
                "reason": "STUCK_TIMEOUT",
            },
        )


_RESOLVER: StuckOrderResolver | None = None


def get_resolver() -> StuckOrderResolver:
    global _RESOLVER
    if _RESOLVER is None:
        _RESOLVER = StuckOrderResolver()
    return _RESOLVER


def setup_stuck_resolver(app) -> None:
    """Register FastAPI lifecycle hooks for the stuck order resolver."""

    resolver = get_resolver()
    runtime.register_stuck_resolver_instance(resolver)

    @app.on_event("startup")
    async def _on_startup() -> None:  # pragma: no cover - lifecycle glue
        runtime.register_stuck_resolver_instance(resolver)
        if not resolver.enabled:
            return
        await resolver.start()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:  # pragma: no cover - lifecycle glue
        try:
            await resolver.stop()
        finally:
            runtime.register_stuck_resolver_instance(None)


__all__ = ["StuckOrderResolver", "get_resolver", "setup_stuck_resolver"]
