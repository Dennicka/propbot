import time
import types
from decimal import Decimal
from typing import Callable

import pytest

from app.router.smart_router import SmartRouter


@pytest.fixture
def router_factory(monkeypatch: pytest.MonkeyPatch) -> Callable[[], SmartRouter]:
    monkeypatch.setattr("app.router.smart_router.ff.pretrade_strict_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.md_watchdog_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.risk_limits_on", lambda: False)
    monkeypatch.setattr(
        "app.router.smart_router.SafeMode.is_active",
        classmethod(lambda cls: False),
    )
    monkeypatch.setattr(
        "app.router.smart_router.provider.get",
        lambda *_: types.SimpleNamespace(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_notional=None,
            min_qty=None,
        ),
    )
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    monkeypatch.setattr(
        "app.router.smart_router.get_profile",
        lambda: types.SimpleNamespace(name="pytest"),
    )
    monkeypatch.setattr("app.router.smart_router.is_live", lambda *_: False)

    def factory() -> SmartRouter:
        state = types.SimpleNamespace(config=None)
        market = types.SimpleNamespace()
        return SmartRouter(state=state, market_data=market)

    return factory


def test_router_tracker_smoke(router_factory: Callable[[], SmartRouter]) -> None:
    router = router_factory()
    order_ids: list[str] = []
    base_ns = time.time_ns()

    for idx in range(100):
        ts_ns = base_ns + idx
        response = router.register_order(
            strategy="test",
            venue="binance",
            symbol="BTCUSDT",
            side="buy",
            qty=1.0,
            price=None,
            ts_ns=ts_ns,
            nonce=idx + 1,
        )
        order_id = str(response["client_order_id"])
        order_ids.append(order_id)
        router.process_order_event(client_order_id=order_id, event="ack")
        router.process_order_event(client_order_id=order_id, event="filled")

    assert len(router._order_tracker) == 0
    stats = router.get_tracker_stats()
    assert stats["removed_terminal"] == len(order_ids)
