from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.risk.budgets import RiskBudgets
from app.router.smart_router import SmartRouter
from app.orders.state import OrderState


@pytest.fixture
def router_with_budgets(monkeypatch: pytest.MonkeyPatch) -> tuple[SmartRouter, RiskBudgets]:
    monkeypatch.setenv("FF_RISK_BUDGETS", "1")
    monkeypatch.setenv("TEST_ONLY_ROUTER_META", "*")
    payload = {
        "xarb-perp": {
            "max_notional_usd": 1000,
            "max_positions": 4,
            "per_symbol_max_notional_usd": {"BTCUSDT": 600},
        }
    }
    monkeypatch.setenv("RISK_BUDGETS_JSON", json.dumps(payload))
    budgets = RiskBudgets()

    monkeypatch.setattr("app.router.smart_router.get_risk_budgets", lambda: budgets)
    monkeypatch.setattr("app.router.smart_router.ff.md_watchdog_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.risk_limits_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.pretrade_strict_on", lambda: False)
    monkeypatch.setattr(
        "app.router.smart_router.SafeMode.is_active", classmethod(lambda cls: False)
    )
    monkeypatch.setattr("app.router.smart_router.get_profile", lambda: SimpleNamespace(name="test"))
    monkeypatch.setattr("app.router.smart_router.is_live", lambda profile: False)

    state = SimpleNamespace(config=SimpleNamespace(data=None))
    market_data = SimpleNamespace()
    router = SmartRouter(state=state, market_data=market_data)
    return router, budgets


def test_risk_budget_blocks_and_releases(
    router_with_budgets: tuple[SmartRouter, RiskBudgets]
) -> None:
    router, budgets = router_with_budgets

    first = router.register_order(
        strategy="xarb-perp",
        venue="bybit",
        symbol="BTCUSDT",
        side="buy",
        qty=0.02,
        price=20000.0,
        ts_ns=1,
        nonce=1,
    )
    first_id = str(first["client_order_id"])
    snapshot = budgets.reg.snapshot()
    assert snapshot["total_by_strategy"]["xarb-perp"] == Decimal("400")

    second = router.register_order(
        strategy="xarb-perp",
        venue="bybit",
        symbol="BTCUSDT",
        side="buy",
        qty=0.02,
        price=20000.0,
        ts_ns=2,
        nonce=2,
    )
    assert second["status"] == "risk_budget_blocked"
    assert second["reason"] == "risk-budget"
    assert second["detail"] == "per_symbol_max_notional_exceeded"

    router.process_order_event(client_order_id=first_id, event="ack")
    router.process_order_event(client_order_id=first_id, event="filled", quantity=0.02)
    snapshot_after = budgets.reg.snapshot()
    assert snapshot_after["total_by_strategy"].get("xarb-perp", Decimal("0")) == Decimal("0")

    third = router.register_order(
        strategy="xarb-perp",
        venue="bybit",
        symbol="BTCUSDT",
        side="buy",
        qty=0.02,
        price=20000.0,
        ts_ns=3,
        nonce=3,
    )
    assert third["client_order_id"]
    assert third["state"] is OrderState.PENDING
