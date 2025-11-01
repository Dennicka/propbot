import importlib

import pytest

from app.router.order_router import OrderRouter
from app.persistence import order_store


class DummyBroker:
    def __init__(self) -> None:
        self.submits: list[str] = []
        self.cancels: list[str] = []
        self.orders: dict[str, str] = {}

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
        assert idemp_key is not None
        self.submits.append(idemp_key)
        broker_id = self.orders.get(idemp_key)
        if broker_id is None:
            broker_id = f"BRK-{len(self.orders) + 1}"
            self.orders[idemp_key] = broker_id
        return {"broker_order_id": broker_id, "idemp_key": idemp_key}

    async def cancel(self, *, venue: str, order_id: str) -> None:
        self.cancels.append(str(order_id))

    async def get_order_by_client_id(self, client_id: str) -> dict[str, object] | None:
        broker_id = self.orders.get(client_id)
        if broker_id is None:
            return None
        return {"broker_order_id": broker_id}


@pytest.fixture(autouse=True)
def _reset_db(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    monkeypatch.setenv("ORDERS_DB_URL", f"sqlite:///{db_path}")
    importlib.reload(order_store)
    try:
        yield
    finally:
        order_store.metadata.drop_all(order_store.get_engine())
        order_store._ENGINE = None  # type: ignore[attr-defined]
        order_store._SESSION_FACTORY = None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_duplicate_submit_returns_same_ref():
    broker = DummyBroker()
    router = OrderRouter(broker)

    ref1 = await router.submit_order(
        account="acct",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        order_type="LIMIT",
        qty=1.0,
        price=10.0,
        request_id="req-1",
    )
    ref2 = await router.submit_order(
        account="acct",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        order_type="LIMIT",
        qty=1.0,
        price=10.0,
        request_id="req-1",
    )

    assert ref1.broker_order_id == ref2.broker_order_id
    assert broker.submits.count("req-1") == 1


@pytest.mark.asyncio
async def test_cancel_is_idempotent():
    broker = DummyBroker()
    router = OrderRouter(broker)

    ref = await router.submit_order(
        account="acct",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        order_type="LIMIT",
        qty=1.0,
        price=10.0,
        request_id="req-cancel",
    )

    await router.cancel_order(
        account="acct",
        venue="binance",
        broker_order_id=ref.broker_order_id,
        request_id="cancel-1",
    )

    await router.cancel_order(
        account="acct",
        venue="binance",
        broker_order_id=ref.broker_order_id,
        request_id="cancel-1",
    )

    assert broker.cancels == [ref.broker_order_id]


@pytest.mark.asyncio
async def test_replace_chain_atomic_and_safe():
    broker = DummyBroker()
    router = OrderRouter(broker)

    ref = await router.submit_order(
        account="acct",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        order_type="LIMIT",
        qty=1.0,
        price=10.0,
        request_id="req-orig",
    )

    new_ref = await router.replace_order(
        account="acct",
        venue="binance",
        broker_order_id=ref.broker_order_id,
        new_params={"qty": 2.0, "price": 11.0},
        request_id="req-repl",
    )

    assert new_ref.intent_id == "req-repl"
    assert broker.submits.count("req-repl") == 1
    assert broker.cancels.count(ref.broker_order_id) == 1

    with order_store.session_scope() as session:
        old_intent = order_store.load_intent(session, "req-orig")
        replacement = order_store.load_intent(session, "req-repl")
    assert old_intent is not None and old_intent.replaced_by == "req-repl"
    assert replacement is not None and replacement.state == order_store.OrderIntentState.ACKED


@pytest.mark.asyncio
async def test_restart_recovers_inflight_intents():
    broker = DummyBroker()
    router = OrderRouter(broker)

    ref = await router.submit_order(
        account="acct",
        venue="binance",
        symbol="ETHUSDT",
        side="buy",
        order_type="LIMIT",
        qty=2.0,
        price=20.0,
        request_id="req-recover",
    )

    with order_store.session_scope() as session:
        intent = order_store.load_intent(session, ref.intent_id)
        intent.state = order_store.OrderIntentState.SENT
        intent.broker_order_id = None
        session.add(intent)

    broker.submits.clear()
    new_router = OrderRouter(broker)
    await new_router.recover_inflight()

    with order_store.session_scope() as session:
        recovered = order_store.load_intent(session, ref.intent_id)
    assert recovered is not None
    assert recovered.state == order_store.OrderIntentState.ACKED
    assert recovered.broker_order_id == "BRK-1"
    assert broker.submits == []


@pytest.mark.asyncio
async def test_adapters_pass_client_order_id():
    broker = DummyBroker()
    router = OrderRouter(broker)

    await router.submit_order(
        account="acct",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        order_type="LIMIT",
        qty=1.0,
        price=10.0,
        request_id="req-client",
    )
    await router.replace_order(
        account="acct",
        venue="binance",
        broker_order_id="BRK-1",
        new_params={"qty": 1.5},
        request_id="req-client-repl",
    )

    assert "req-client" in broker.orders
    assert "req-client-repl" in broker.orders
