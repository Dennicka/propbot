import asyncio
from types import SimpleNamespace

import pytest

from app.services import partial_hedge_runner
from app.services.partial_hedge_runner import PartialHedgeRunner, reset_state_for_tests


class DummyPlanner:
    def __init__(self, orders=None):
        self.orders = orders or [
            {"venue": "binance", "symbol": "BTCUSDT", "side": "SELL", "qty": 1.0, "reason": "test"}
        ]
        self.last_plan_details = {}
        self.calls = 0

    def plan(self, residuals):
        self.calls += 1
        self.last_plan_details = {
            "generated_ts": "2024-01-01T00:00:00Z",
            "orders": [dict(order, notional_usdt=1_000.0) for order in self.orders],
            "totals": {"orders": len(self.orders), "notional_usdt": 1_000.0 * len(self.orders)},
            "symbols": {},
        }
        return [dict(order) for order in self.orders]


@pytest.fixture(autouse=True)
def _reset_state():
    reset_state_for_tests()


@pytest.mark.asyncio
async def test_runner_dry_run_only_records_plan(monkeypatch):
    planner = DummyPlanner()
    captured_orders = []

    async def fake_executor(orders):
        captured_orders.append(list(orders))
        return []

    dummy_state = SimpleNamespace(
        control=SimpleNamespace(safe_mode=False, two_man_rule=False, approvals={})
    )
    monkeypatch.setattr(partial_hedge_runner, "get_state", lambda: dummy_state)
    monkeypatch.setattr(partial_hedge_runner, "is_hold_active", lambda: False)
    monkeypatch.setattr(partial_hedge_runner, "register_order_attempt", lambda **_: None)

    runner = PartialHedgeRunner(
        planner=planner,
        residuals_provider=lambda: asyncio.sleep(0, result=[]),
        order_executor=fake_executor,
        enabled=True,
        dry_run=True,
    )

    snapshot = await runner.run_cycle()

    assert snapshot["status"] == "planned"
    assert captured_orders == []
    assert planner.calls == 1


@pytest.mark.asyncio
async def test_runner_executes_orders_with_two_man_rule(monkeypatch):
    planner = DummyPlanner()
    executed = []

    async def fake_executor(orders):
        executed.append(list(orders))
        return [{"status": "filled"}]

    control = SimpleNamespace(safe_mode=False, two_man_rule=True, approvals={"a": "1", "b": "2"})
    dummy_state = SimpleNamespace(control=control)
    monkeypatch.setattr(partial_hedge_runner, "get_state", lambda: dummy_state)
    monkeypatch.setattr(partial_hedge_runner, "is_hold_active", lambda: False)
    monkeypatch.setattr(partial_hedge_runner, "register_order_attempt", lambda **_: None)

    runner = PartialHedgeRunner(
        planner=planner,
        residuals_provider=lambda: asyncio.sleep(0, result=[]),
        order_executor=fake_executor,
        enabled=True,
        dry_run=False,
    )

    snapshot = await runner.plan_once(execute=True)

    assert executed and executed[0][0]["venue"] == "binance"
    assert snapshot["execution"]["status"] == "executed"


@pytest.mark.asyncio
async def test_runner_blocks_when_two_man_rule_missing(monkeypatch):
    planner = DummyPlanner()
    control = SimpleNamespace(safe_mode=False, two_man_rule=True, approvals={"only": "one"})
    dummy_state = SimpleNamespace(control=control)
    monkeypatch.setattr(partial_hedge_runner, "get_state", lambda: dummy_state)
    monkeypatch.setattr(partial_hedge_runner, "is_hold_active", lambda: False)
    monkeypatch.setattr(partial_hedge_runner, "register_order_attempt", lambda **_: None)

    runner = PartialHedgeRunner(
        planner=planner,
        residuals_provider=lambda: asyncio.sleep(0, result=[]),
        order_executor=lambda orders: asyncio.sleep(0, result=[]),
        enabled=True,
        dry_run=False,
    )

    snapshot = await runner.plan_once(execute=True)
    assert snapshot["execution"]["status"] == "blocked"
    assert snapshot["execution"]["reason"] == "two_man_rule_missing"


@pytest.mark.asyncio
async def test_runner_triggers_auto_hold_after_repeated_failures(monkeypatch):
    planner = DummyPlanner()
    control = SimpleNamespace(safe_mode=False, two_man_rule=False, approvals={})
    dummy_state = SimpleNamespace(control=control)
    monkeypatch.setattr(partial_hedge_runner, "get_state", lambda: dummy_state)
    monkeypatch.setattr(partial_hedge_runner, "is_hold_active", lambda: False)
    monkeypatch.setattr(partial_hedge_runner, "register_order_attempt", lambda **_: None)
    engaged = []
    monkeypatch.setattr(
        partial_hedge_runner,
        "engage_safety_hold",
        lambda reason, source=None: engaged.append((reason, source)),
    )

    async def failing_executor(orders):
        raise RuntimeError("insufficient balance")

    runner = PartialHedgeRunner(
        planner=planner,
        residuals_provider=lambda: asyncio.sleep(0, result=[]),
        order_executor=failing_executor,
        enabled=True,
        dry_run=False,
    )

    for _ in range(4):
        await runner.plan_once(execute=True)

    assert engaged
    reason, source = engaged[-1]
    assert reason == "partial_hedge:auto_hold"
    assert source == "partial_hedge"
