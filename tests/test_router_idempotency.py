import importlib

import pytest

from app.router.order_router import OrderRouter
from app.persistence import order_store


class DummyBroker:
    def __init__(self) -> None:
        self.submits: list[str] = []
        self.orders: dict[str, str] = {}
        self.lookup: dict[str, dict[str, str]] = {}
        self.client_queries: list[str] = []

    async def create_order(self, *, idemp_key: str | None = None, **_):
        assert idemp_key is not None
        self.submits.append(idemp_key)
        broker_id = self.orders.get(idemp_key)
        if broker_id is None:
            broker_id = f"BRK-{len(self.orders) + 1}"
            self.orders[idemp_key] = broker_id
        record = {"broker_order_id": broker_id, "idemp_key": idemp_key}
        self.lookup[idemp_key] = record
        return record

    async def cancel(self, **_):  # pragma: no cover - not used
        return None

    async def get_order_by_client_id(self, client_id: str):
        self.client_queries.append(client_id)
        return self.lookup.get(client_id)


@pytest.fixture(autouse=True)
def _reset_order_store(tmp_path, monkeypatch):
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
async def test_retry_keeps_same_intent_id():
    broker = DummyBroker()
    router = OrderRouter(broker)

    ref1 = await router.submit_order(
        account="acct",
        venue="test-venue",
        symbol="BTCUSDT",
        side="buy",
        order_type="LIMIT",
        qty=1.0,
        price=10.0,
        request_id="intent-1",
    )

    ref2 = await router.submit_order(
        account="acct",
        venue="test-venue",
        symbol="BTCUSDT",
        side="buy",
        order_type="LIMIT",
        qty=1.0,
        price=10.0,
        intent_id=ref1.intent_id,
        request_id="retry-1",
    )

    assert ref2.intent_id == ref1.intent_id
    assert ref2.request_id == "retry-1"
    assert broker.submits == ["intent-1", "retry-1"]

    with order_store.session_scope() as session:
        intent = order_store.load_intent(session, ref1.intent_id)
    assert intent is not None
    assert intent.intent_id == ref1.intent_id
    assert intent.request_id == "retry-1"

    with order_store.session_scope() as session:
        intent = order_store.load_intent(session, ref1.intent_id)
        assert intent is not None
        intent.request_id = "retry-2"
        intent.state = order_store.OrderIntentState.PENDING
        intent.broker_order_id = None
        session.add(intent)

    broker.lookup["retry-2"] = {"broker_order_id": "BRK-77"}

    await router.recover_inflight()

    assert broker.client_queries == ["retry-2"]

    with order_store.session_scope() as session:
        recovered = order_store.load_intent(session, ref1.intent_id)
    assert recovered is not None
    assert recovered.request_id == "retry-2"
    assert recovered.broker_order_id == "BRK-77"
    assert recovered.state == order_store.OrderIntentState.ACKED

