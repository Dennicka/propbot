import asyncio
from dataclasses import dataclass
from typing import Iterable, Tuple

import pytest

from app.orders.state import OrderState
from app.recon.core import ReconConfig, ReconReport, Reconciler
from app.recon.daemon import OrderReconDaemon


@dataclass
class FakeTrackedOrder:
    order_id: str
    state: OrderState
    last_update_ts: float


class FakeRouter:
    def __init__(self, orders: Iterable[FakeTrackedOrder]) -> None:
        self._orders = list(orders)
        self.purged = 0
        self.purge_calls = 0

    def snapshot_tracked_orders(self) -> Tuple[FakeTrackedOrder, ...]:
        return tuple(self._orders)

    def update(
        self, order_id: str, *, last_update_ts: float | None = None, state: OrderState | None = None
    ) -> None:
        for order in self._orders:
            if order.order_id == order_id:
                if last_update_ts is not None:
                    order.last_update_ts = last_update_ts
                if state is not None:
                    order.state = state
                break

    def purge_terminal_orders(self, *, ttl_sec: int, now_ts: float | None = None) -> int:
        threshold = float(now_ts) if now_ts is not None else 0.0
        ttl = float(ttl_sec)
        retained: list[FakeTrackedOrder] = []
        removed = 0
        for order in self._orders:
            if (
                order.state
                in {
                    OrderState.FILLED,
                    OrderState.CANCELED,
                    OrderState.REJECTED,
                    OrderState.EXPIRED,
                }
                and threshold - order.last_update_ts > ttl
            ):
                removed += 1
                continue
            retained.append(order)
        self._orders = retained
        self.purged += removed
        self.purge_calls += 1
        return removed


class FakeClock:
    def __init__(self, start: float) -> None:
        self._value = start

    def __call__(self) -> float:
        return self._value

    def set(self, value: float) -> None:
        self._value = value

    def advance(self, delta: float) -> None:
        self._value += delta


@dataclass(slots=True)
class ReconConfigWithGC(ReconConfig):
    gc_ttl_sec: int = 300


def _base_orders(now: float) -> list[FakeTrackedOrder]:
    return [
        FakeTrackedOrder(order_id="fresh", state=OrderState.ACK, last_update_ts=now - 1),
        FakeTrackedOrder(order_id="stale", state=OrderState.PARTIAL, last_update_ts=now - 10),
        FakeTrackedOrder(order_id="final", state=OrderState.FILLED, last_update_ts=now - 100),
    ]


def test_reconciler_flags_stale_orders() -> None:
    start = 100.0
    clock = FakeClock(start)
    router = FakeRouter(_base_orders(start))
    cfg = ReconConfig(order_stale_sec=5.0, max_batch=10)
    reconciler = Reconciler(router=router, clock=clock, cfg=cfg)

    report = reconciler.check_staleness()

    assert report.ts == pytest.approx(start)
    assert report.checked == 2
    assert {issue.order_id for issue in report.issues} == {"stale"}
    issue = report.issues[0]
    assert issue.kind == "stale-order"
    assert issue.age_sec is not None and issue.age_sec > cfg.order_stale_sec
    assert issue.details and issue.details.get("state") == OrderState.PARTIAL.value

    router.update("stale", last_update_ts=start)
    clock.set(start + 1)
    refreshed = reconciler.check_staleness()
    assert all(issue.order_id != "stale" for issue in refreshed.issues)


@pytest.mark.asyncio
async def test_daemon_emits_reports_and_updates_state() -> None:
    start = 200.0
    clock = FakeClock(start)
    router = FakeRouter(_base_orders(start))
    cfg = ReconConfig(order_stale_sec=5.0, interval_sec=0.01)
    reconciler = Reconciler(router=router, clock=clock, cfg=cfg)
    daemon = OrderReconDaemon(reconciler)

    clock.set(start + 10)
    router.update("fresh", last_update_ts=clock())
    await daemon.start()
    try:
        await asyncio.sleep(cfg.interval_sec * 3)
        report = await daemon.get_last_report()
        assert report is not None
        assert report.checked == 2
        assert any(issue.order_id == "stale" for issue in report.issues)

        router.update("stale", last_update_ts=clock())
        clock.advance(1)
        refreshed: ReconReport | None = None
        for _ in range(10):
            await asyncio.sleep(cfg.interval_sec)
            candidate = await daemon.get_last_report()
            if candidate is not None and candidate.ts > report.ts:
                refreshed = candidate
                break
        assert refreshed is not None
        assert all(issue.order_id != "stale" for issue in refreshed.issues)
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_daemon_triggers_gc_for_terminal_orders() -> None:
    start = 500.0
    clock = FakeClock(start)
    router = FakeRouter(
        [
            FakeTrackedOrder(
                order_id="recent-final", state=OrderState.FILLED, last_update_ts=start - 10
            ),
            FakeTrackedOrder(
                order_id="old-final", state=OrderState.CANCELED, last_update_ts=start - 400
            ),
            FakeTrackedOrder(order_id="active", state=OrderState.ACK, last_update_ts=start - 1),
        ]
    )
    cfg = ReconConfigWithGC(order_stale_sec=5.0, interval_sec=0.01, gc_ttl_sec=100)
    reconciler = Reconciler(router=router, clock=clock, cfg=cfg)
    daemon = OrderReconDaemon(reconciler)

    await daemon.start()
    try:
        await asyncio.sleep(cfg.interval_sec * 5)
        assert router.purge_calls > 0
        assert router.purged >= 1
        remaining = {order.order_id for order in router.snapshot_tracked_orders()}
        assert "old-final" not in remaining
        assert "recent-final" in remaining
        assert "active" in remaining
    finally:
        await daemon.stop()
