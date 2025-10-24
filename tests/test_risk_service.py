from __future__ import annotations

from datetime import datetime, timezone

from datetime import datetime, timezone

from app import ledger
from app.services import risk
from app.services.arbitrage import Plan, PlanLeg
from app.services.runtime import get_state, reset_for_tests


def _make_plan(symbol: str = "BTCUSDT", notional: float = 100.0) -> Plan:
    plan = Plan(
        symbol=symbol,
        notional=notional,
        used_slippage_bps=0,
        used_fees_bps={"binance": 0, "okx": 0},
        viable=True,
    )
    qty_buy = notional / 20_000.0
    qty_sell = notional / 20_050.0 if notional else 0.0
    plan.legs = [
        PlanLeg(exchange="binance", side="buy", price=20_000.0, qty=qty_buy, fee_usdt=0.0),
        PlanLeg(exchange="okx", side="sell", price=20_050.0, qty=qty_sell, fee_usdt=0.0),
    ]
    return plan


def setup_function() -> None:
    reset_for_tests()
    ledger.reset()


def test_plan_rejected_by_position_limit() -> None:
    state = get_state()
    state.risk.limits.max_position_usdt = {"BTCUSDT": 50.0}
    plan = _make_plan(notional=200.0)
    risk.evaluate_plan(plan)
    assert plan.viable is False
    assert plan.reason and "max_position_usdt" in plan.reason


def test_plan_rejected_by_open_order_limit() -> None:
    state = get_state()
    state.risk.limits.max_position_usdt = {}
    state.risk.limits.max_open_orders = {"__default__": 0}
    plan = _make_plan(notional=100.0)
    risk.evaluate_plan(plan)
    assert plan.viable is False
    assert plan.reason and "max_open_orders" in plan.reason


def test_plan_rejected_by_daily_loss_limit() -> None:
    state = get_state()
    state.risk.limits.max_position_usdt = {}
    state.risk.limits.max_open_orders = {}
    state.risk.limits.max_daily_loss_usdt = 50.0
    ts = datetime.now(timezone.utc).isoformat()
    buy_order = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=20_000.0,
        status="filled",
        client_ts=ts,
        exchange_ts=ts,
        idemp_key="risk-buy",
    )
    ledger.record_fill(
        order_id=buy_order,
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=20_000.0,
        fee=0.0,
        ts=ts,
    )
    sell_order = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="sell",
        qty=1.0,
        price=19_900.0,
        status="filled",
        client_ts=ts,
        exchange_ts=ts,
        idemp_key="risk-sell",
    )
    ledger.record_fill(
        order_id=sell_order,
        venue="binance-um",
        symbol="BTCUSDT",
        side="sell",
        qty=1.0,
        price=19_900.0,
        fee=0.0,
        ts=ts,
    )

    plan = _make_plan(notional=100.0)
    risk.evaluate_plan(plan)
    assert plan.viable is False
    assert plan.reason and "max_daily_loss_usdt" in plan.reason
    assert any(b.limit == "max_daily_loss_usdt" for b in state.risk.breaches)


def test_risk_state_metrics_shape_and_limits() -> None:
    state = get_state()
    state.risk.limits.max_position_usdt = {"BTCUSDT": 100.0}
    state.risk.limits.max_open_orders = {"__default__": 1}
    state.risk.limits.max_daily_loss_usdt = 1_000.0
    ts = datetime.now(timezone.utc).isoformat()
    order_id = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.5,
        price=20_000.0,
        status="filled",
        client_ts=ts,
        exchange_ts=ts,
        idemp_key="risk-metrics",
    )
    ledger.record_fill(
        order_id=order_id,
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.5,
        price=20_000.0,
        fee=0.0,
        ts=ts,
    )
    overview = risk.risk_overview()
    assert set(overview.keys()) >= {"limits", "current", "breaches", "positions_usdt", "exposures", "exposure_totals", "limits_hit"}
    assert overview["positions_usdt"].get("BTCUSDT", 0.0) > 0
    assert any(entry["symbol"] == "BTCUSDT" for entry in overview["exposures"])
