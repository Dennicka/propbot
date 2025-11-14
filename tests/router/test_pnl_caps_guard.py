from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.orders.state import OrderState
from app.risk.pnl_caps import FillEvent
from app.router import smart_router as smart_router_module
from app.router.smart_router import SmartRouter


class FakeTime:
    def __init__(self, start: float) -> None:
        self._now = start
        self._perf = 0.0

    def time(self) -> float:
        return self._now

    def time_ns(self) -> int:
        return int(self._now * 1_000_000_000)

    def perf_counter(self) -> float:
        return self._perf

    def advance(self, seconds: float) -> None:
        self._now += seconds
        self._perf += seconds


@pytest.fixture
def router_with_pnl_caps(monkeypatch: pytest.MonkeyPatch) -> tuple[SmartRouter, FakeTime]:
    monkeypatch.setenv("FF_DAILY_LOSS_CAP", "1")
    monkeypatch.setenv("PNL_TZ", "UTC")
    monkeypatch.setenv("DAILY_LOSS_CAP_USD_GLOBAL", "50")
    monkeypatch.setenv("INTRADAY_DRAWDOWN_CAP_USD_GLOBAL", "0")
    monkeypatch.setenv("PNL_CAPS_COOLOFF_MIN", "1")
    monkeypatch.setenv("PNL_CAPS_REPORT_EVERY_SEC", "0")
    monkeypatch.setenv("TEST_ONLY_ROUTER_META", "*")

    fake_time = FakeTime(1704067200.0)
    monkeypatch.setattr(smart_router_module, "time", fake_time)
    monkeypatch.setattr("app.router.smart_router.ff.md_watchdog_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.risk_limits_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.pretrade_strict_on", lambda: False)
    monkeypatch.setattr(
        "app.router.smart_router.SafeMode.is_active", classmethod(lambda cls: False)
    )
    monkeypatch.setattr("app.router.smart_router.get_profile", lambda: SimpleNamespace(name="test"))
    monkeypatch.setattr("app.router.smart_router.is_live", lambda profile: False)
    monkeypatch.setattr("app.router.smart_router.metrics_write", lambda path: None)

    state = SimpleNamespace(config=SimpleNamespace(data=None))
    market_data = SimpleNamespace()
    router = SmartRouter(state=state, market_data=market_data)
    router._pnl_guard.clock = fake_time
    return router, fake_time


def _fill_order(router: SmartRouter, order_id: str, pnl: Decimal) -> None:
    smart_router_module.time.advance(1.0)
    router.process_order_event(client_order_id=order_id, event="ack")
    smart_router_module.time.advance(1.0)
    router.process_order_event(
        client_order_id=order_id,
        event="filled",
        quantity=0.01,
        realized_pnl_usd=pnl,
    )


def test_guard_blocks_after_daily_cap_and_resumes(router_with_pnl_caps) -> None:
    router, fake_time = router_with_pnl_caps
    smart_router_module._PNLCAP_BLOCKS_TOTAL._values.clear()

    first = router.register_order(
        strategy="auto_hedge",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=0.01,
        price=20000.0,
        ts_ns=1,
        nonce=1,
    )
    first_id = str(first["client_order_id"])
    assert first["state"] is OrderState.PENDING
    _fill_order(router, first_id, Decimal("-30"))

    second = router.register_order(
        strategy="auto_hedge",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=0.01,
        price=19950.0,
        ts_ns=2,
        nonce=2,
    )
    second_id = str(second["client_order_id"])
    assert second["state"] is OrderState.PENDING
    _fill_order(router, second_id, Decimal("-25"))

    third = router.register_order(
        strategy="auto_hedge",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=0.01,
        price=19900.0,
        ts_ns=3,
        nonce=3,
    )
    assert third == {
        "ok": False,
        "reason": "pnl-cap",
        "detail": "daily-loss-cap-global",
        "cost": None,
    }
    block_key = ("daily-loss-cap-global", "auto_hedge")
    assert smart_router_module._PNLCAP_BLOCKS_TOTAL._values.get(block_key) == pytest.approx(1.0)

    # Cooloff expiry combined with recovery trading allows orders again.
    fake_time.advance(61.0)
    router._pnl_agg.on_fill(
        FillEvent(
            t=fake_time.time(),
            strategy="auto_hedge",
            symbol="BTCUSDT",
            realized_pnl_usd=Decimal("15"),
        )
    )
    router._update_pnl_metrics(fake_time.time())

    fourth = router.register_order(
        strategy="auto_hedge",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=0.01,
        price=19850.0,
        ts_ns=4,
        nonce=4,
    )
    assert fourth["state"] is OrderState.PENDING
