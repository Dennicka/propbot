import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.execution.stuck_order_resolver import StuckOrderResolver
from app.router.order_router import OrderRouter
from app.persistence import order_store


class DummyBroker:
    def __init__(self) -> None:
        self.submits: list[str] = []
        self.cancels: list[str] = []
        self._orders: dict[str, str] = {}

    async def create_order(self, *, idemp_key: str | None = None, **params):
        assert idemp_key is not None
        self.submits.append(idemp_key)
        broker_id = self._orders.get(idemp_key)
        if broker_id is None:
            broker_id = f"BRK-{len(self._orders) + 1}"
            self._orders[idemp_key] = broker_id
        return {"broker_order_id": broker_id, "idemp_key": idemp_key}

    async def cancel(self, *, order_id: str | None = None, **_):
        if order_id is not None:
            self.cancels.append(str(order_id))


class DummyRuntime:
    def __init__(self, *, pending_timeout: float = 0.0, max_retries: int = 3) -> None:
        config = SimpleNamespace(
            enabled=True,
            pending_timeout_sec=pending_timeout,
            cancel_grace_sec=0.0,
            max_retries=max_retries,
            backoff_sec=[0.0],
        )
        self.state = SimpleNamespace(execution=SimpleNamespace(stuck_resolver=config))
        self.retries: list[tuple[str, float | None]] = []
        self.incidents: list[tuple[str, dict]] = []
        self.errors: dict[str, str] = {}

    def get_state(self):
        return self.state

    def record_stuck_resolver_retry(self, *, intent_id: str, timestamp: float | None = None) -> None:
        self.retries.append((intent_id, timestamp))

    def record_incident(self, kind: str, details: dict) -> None:
        self.incidents.append((kind, dict(details)))

    def record_stuck_resolver_error(self, intent_id: str, error: str) -> None:
        self.errors[intent_id] = error

    def clear_stuck_resolver_error(self, intent_id: str) -> None:
        self.errors.pop(intent_id, None)


class FakeLedger:
    def __init__(self, orders: list[dict], statuses: dict[int, dict] | None = None) -> None:
        self.orders = list(orders)
        self.statuses = statuses or {}
        self.fills: list[dict] = []

    def fetch_open_orders(self):
        return list(self.orders)

    def fetch_fills_since(self, since):
        return list(self.fills)

    def get_order(self, order_id: int):
        return dict(self.statuses.get(order_id, {}))


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
async def test_cancels_and_retries_after_timeout(monkeypatch):
    broker = DummyBroker()
    router = OrderRouter(broker)
    order_ref = await router.submit_order(
        account="acct",
        venue="test-venue",
        symbol="BTCUSDT",
        side="buy",
        order_type="LIMIT",
        qty=1.0,
        price=10.0,
        request_id="intent-1",
    )
    intent_id = order_ref.intent_id
    broker_order_id = order_ref.broker_order_id
    assert broker_order_id is not None

    now = datetime.now(timezone.utc)
    open_orders = [
        {
            "id": 1,
            "status": "submitted",
            "client_ts": (now - timedelta(seconds=10)).isoformat(),
            "idemp_key": intent_id,
            "venue": "test-venue",
            "symbol": "BTCUSDT",
        }
    ]
    ledger = FakeLedger(open_orders, {1: {"status": "submitted"}})
    runtime_stub = DummyRuntime(pending_timeout=1.0)

    resolver = StuckOrderResolver(ctx=runtime_stub, order_router=router)
    resolver._ledger = ledger
    resolver._last_fill_poll = None

    await resolver.run_once()

    assert broker.cancels == [broker_order_id]
    assert len(broker.submits) == 2
    assert broker.submits[0] == intent_id
    assert broker.submits[1] != intent_id
    assert runtime_stub.retries and runtime_stub.retries[0][0] == intent_id
    assert runtime_stub.incidents and runtime_stub.incidents[0][1]["reason"] == "STUCK_TIMEOUT"
    assert resolver._retry_counts[intent_id] == 1

    with order_store.session_scope() as session:
        intent = order_store.load_intent(session, intent_id)
    assert intent is not None
    assert intent.intent_id == intent_id
    assert intent.request_id == broker.submits[1]


@pytest.mark.asyncio
async def test_does_not_retry_when_filled(monkeypatch):
    broker = DummyBroker()
    router = OrderRouter(broker)
    order_ref = await router.submit_order(
        account="acct",
        venue="test-venue",
        symbol="BTCUSDT",
        side="buy",
        order_type="LIMIT",
        qty=1.0,
        price=10.0,
        request_id="intent-fill",
    )
    intent_id = order_ref.intent_id
    broker_order_id = order_ref.broker_order_id
    assert broker_order_id is not None
    now = datetime.now(timezone.utc)
    ledger = FakeLedger(
        [
            {
                "id": 42,
                "status": "submitted",
                "client_ts": (now - timedelta(seconds=10)).isoformat(),
                "idemp_key": intent_id,
                "venue": "test-venue",
                "symbol": "BTCUSDT",
            }
        ],
        {42: {"status": "submitted"}},
    )
    ledger.fills = [
        {
            "order_id": 42,
            "ts": (now - timedelta(seconds=1)).isoformat(),
        }
    ]
    runtime_stub = DummyRuntime(pending_timeout=1.0)
    resolver = StuckOrderResolver(ctx=runtime_stub, order_router=router)
    resolver._ledger = ledger
    resolver._last_fill_poll = None

    await resolver.run_once()

    assert broker.cancels == []
    assert broker.submits == [intent_id]
    assert runtime_stub.retries == []


@pytest.mark.asyncio
async def test_respects_max_retries_and_sets_error(monkeypatch):
    broker = DummyBroker()
    router = OrderRouter(broker)
    order_ref = await router.submit_order(
        account="acct",
        venue="test-venue",
        symbol="BTCUSDT",
        side="buy",
        order_type="LIMIT",
        qty=1.0,
        price=10.0,
        request_id="intent-max",
    )
    intent_id = order_ref.intent_id
    now = datetime.now(timezone.utc)
    ledger = FakeLedger(
        [
            {
                "id": 7,
                "status": "submitted",
                "client_ts": (now - timedelta(seconds=10)).isoformat(),
                "idemp_key": intent_id,
                "venue": "test-venue",
                "symbol": "BTCUSDT",
            }
        ],
        {7: {"status": "submitted"}},
    )
    runtime_stub = DummyRuntime(pending_timeout=1.0, max_retries=1)
    resolver = StuckOrderResolver(ctx=runtime_stub, order_router=router)
    resolver._ledger = ledger
    resolver._last_fill_poll = None

    await resolver.run_once()
    await resolver.run_once()

    assert runtime_stub.errors.get(intent_id) == "STUCK_MAX_RETRIES"
    assert any(reason.get("reason") == "STUCK_MAX_RETRIES" for _, reason in runtime_stub.incidents)
    assert len([submit for submit in broker.submits if submit != intent_id]) == 1
    assert len(runtime_stub.retries) == 1

