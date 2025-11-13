from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.risk.budgets import RiskBudgets


@pytest.fixture
def budgets(monkeypatch: pytest.MonkeyPatch) -> RiskBudgets:
    payload = {
        "xarb-perp": {
            "max_notional_usd": 200,
            "max_positions": 2,
            "per_symbol_max_notional_usd": {"BTCUSDT": 120, "ETHUSDT": 120},
        }
    }
    monkeypatch.setenv("RISK_BUDGETS_JSON", json.dumps(payload))
    monkeypatch.setenv("RISK_BUDGETS_TTL_SEC", "5")
    monkeypatch.setenv("RISK_BUDGETS_MAX_RESERVATIONS", "50")
    return RiskBudgets()


def test_can_accept_then_block_by_symbol_limit(budgets: RiskBudgets) -> None:
    ok, reason = budgets.can_accept("xarb-perp", "BTCUSDT", Decimal("60"))
    assert ok
    assert reason == "ok"

    budgets.reg.reserve("order-1", "xarb-perp", "BTCUSDT", Decimal("60"))
    ok_second, reason_second = budgets.can_accept("xarb-perp", "BTCUSDT", Decimal("70"))
    assert not ok_second
    assert reason_second == "per_symbol_max_notional_exceeded"


def test_max_positions_enforced(budgets: RiskBudgets) -> None:
    budgets.reg.reserve("order-1", "xarb-perp", "BTCUSDT", Decimal("60"))
    ok, reason = budgets.can_accept("xarb-perp", "ETHUSDT", Decimal("50"))
    assert ok
    budgets.reg.reserve("order-2", "xarb-perp", "ETHUSDT", Decimal("50"))

    ok_new, reason_new = budgets.can_accept("xarb-perp", "LTCUSDT", Decimal("10"))
    assert not ok_new
    assert reason_new == "max_positions_exceeded"


def test_cleanup_expires_reservations(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "xarb-perp": {
            "max_notional_usd": 500,
            "max_positions": 5,
            "per_symbol_max_notional_usd": {"BTCUSDT": 500},
        }
    }
    monkeypatch.setenv("RISK_BUDGETS_JSON", json.dumps(payload))
    monkeypatch.setenv("RISK_BUDGETS_TTL_SEC", "1")
    budgets = RiskBudgets()

    budgets.reg.reserve(
        "order-ttl",
        "xarb-perp",
        "BTCUSDT",
        Decimal("100"),
        now=0.0,
    )
    removed = budgets.reg.cleanup(now=10.0)
    assert removed == 1
    snapshot = budgets.reg.snapshot()
    assert snapshot["total_by_strategy"] == {}
