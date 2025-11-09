import importlib

import pytest

from app.router.order_router import OrderRouter, PretradeGateThrottled
from app.persistence import order_store
from app.services import runtime
from app.risk.freeze import FreezeRule, get_freeze_registry, reset_freeze_registry


class DummyBroker:
    def __init__(self) -> None:
        self.submits: list[dict[str, object]] = []

    async def create_order(
        self,
        *,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: float | None = None,
        type: str = "LIMIT",
        tif: str | None = None,
        strategy: str | None = None,
        post_only: bool = True,
        reduce_only: bool = False,
        fee: float = 0.0,
        idemp_key: str | None = None,
    ) -> dict[str, object]:
        payload = {
            "venue": venue,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "type": type,
            "tif": tif,
            "strategy": strategy,
            "reduce_only": reduce_only,
            "idemp_key": idemp_key,
        }
        self.submits.append(payload)
        return {"broker_order_id": f"{venue}-freeze", "idemp_key": idemp_key}

    async def cancel(self, *, venue: str, order_id: str) -> None:  # pragma: no cover - unused
        return None


@pytest.fixture(autouse=True)
def _reset_state(tmp_path, monkeypatch):
    db_path = tmp_path / "orders-freeze.db"
    monkeypatch.setenv("ORDERS_DB_URL", f"sqlite:///{db_path}")
    importlib.reload(order_store)
    runtime.reset_for_tests()
    reset_freeze_registry()
    gate = runtime.get_pre_trade_gate()
    gate.clear()
    try:
        yield
    finally:
        gate.clear()
        reset_freeze_registry()
        order_store.metadata.drop_all(order_store.get_engine())
        order_store._ENGINE = None  # type: ignore[attr-defined]
        order_store._SESSION_FACTORY = None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_frozen_scope_blocks_opening(monkeypatch) -> None:
    registry = get_freeze_registry()
    registry.apply(FreezeRule(reason="RECON_CRITICAL::venue=binance", scope="venue", ts=1.0))

    broker = DummyBroker()
    router = OrderRouter(broker)
    monkeypatch.setattr(router, "_current_position", lambda venue, symbol: 0.0)

    with pytest.raises(PretradeGateThrottled) as exc:
        await router.submit_order(
            account="acct",
            venue="binance-um",
            symbol="BTCUSDT",
            side="buy",
            order_type="LIMIT",
            qty=1.0,
            price=100.0,
        )

    assert exc.value.reason == "blocked: FROZEN_BY_RISK"
    assert broker.submits == []


@pytest.mark.asyncio
async def test_reduce_only_allowed_when_frozen(monkeypatch) -> None:
    registry = get_freeze_registry()
    registry.apply(
        FreezeRule(
            reason="RECON_CRITICAL::venue=binance::symbol=BTCUSDT",
            scope="symbol",
            ts=1.0,
        )
    )

    broker = DummyBroker()
    router = OrderRouter(broker)
    monkeypatch.setattr(router, "_current_position", lambda venue, symbol: 2.0)
    monkeypatch.setattr(router, "_supports_native_reduce_only", lambda venue: True)

    ref = await router.submit_order(
        account="acct",
        venue="binance-um",
        symbol="BTCUSDT",
        side="sell",
        order_type="LIMIT",
        qty=1.5,
        price=100.0,
    )

    assert ref.broker_order_id == "binance-um-freeze"
    assert broker.submits[-1]["reduce_only"] is True
