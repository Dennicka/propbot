import math

import pytest

from app.hedge.partial import PartialHedgePlanner


@pytest.fixture
def planner_factory():
    positions: list[dict[str, object]] = []
    balances: list[dict[str, object]] = []

    def build(*, pos=None, bal=None, **kwargs):
        positions.clear()
        positions.extend([dict(row) for row in (pos or [])])
        balances.clear()
        balances.extend([dict(row) for row in (bal or [])])
        min_notional = kwargs.pop("min_notional", None)
        max_notional = kwargs.pop("max_notional_per_order", None)
        return PartialHedgePlanner(
            positions_fetcher=lambda: list(positions),
            balances_fetcher=lambda: list(balances),
            min_notional_usdt=min_notional,
            max_notional_usdt_per_order=max_notional,
            **kwargs,
        )

    return build


def test_selects_best_venue_by_expected_cost(planner_factory):
    positions = [
        {"venue": "okx", "symbol": "BTCUSDT", "base_qty": 0.7, "avg_price": 20_000.0}
    ]
    balances = [
        {"venue": "binance", "asset": "USDT", "qty": 20_000.0},
        {"venue": "okx", "asset": "USDT", "qty": 5_000.0},
    ]
    planner = planner_factory(
        pos=positions,
        bal=balances,
        min_notional=100.0,
        max_notional_per_order=10_000.0,
    )
    residuals = [
        {
            "venue": "OKX",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "qty": 0.5,
            "strategy": "arb-x",
            "notional_usdt": 10_000.0,
            "funding_apr": 0.10,
            "taker_fee_bps": 4.0,
            "maker_fee_bps": 1.0,
        },
        {
            "venue": "BINANCE",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "qty": 0.2,
            "strategy": "arb-y",
            "notional_usdt": 4_000.0,
            "funding_apr": 0.02,
            "taker_fee_bps": 2.0,
            "maker_fee_bps": 0.5,
        },
    ]

    orders = planner.plan(residuals)

    assert len(orders) == 2
    assert all(order["venue"] == "binance" for order in orders)
    total_qty = sum(order["qty"] for order in orders)
    assert math.isclose(total_qty, 0.7, rel_tol=1e-6)
    details = planner.last_plan_details
    assert details["totals"]["orders"] == 2
    assert details["totals"]["notional_usdt"] == pytest.approx(14_000.0)


def test_split_respects_balance_and_max(planner_factory):
    positions = [
        {"venue": "binance", "symbol": "ETHUSDT", "base_qty": -10.0, "avg_price": 1_000.0}
    ]
    balances = [
        {"venue": "binance", "asset": "USDT", "qty": 6_000.0},
    ]
    planner = planner_factory(
        pos=positions,
        bal=balances,
        min_notional=100.0,
        max_notional_per_order=2_500.0,
    )
    residuals = [
        {
            "venue": "BINANCE",
            "symbol": "ETHUSDT",
            "side": "SHORT",
            "qty": 10.0,
            "strategy": "hedge-short",
            "notional_usdt": 10_000.0,
            "funding_apr": 0.0,
            "taker_fee_bps": 2.0,
            "maker_fee_bps": 0.5,
        }
    ]

    orders = planner.plan(residuals)

    assert len(orders) == 3
    qtys = [order["qty"] for order in orders]
    assert pytest.approx(sum(qtys), rel=1e-6) == 6.0  # limited by balance (6k notional)
    assert qtys[0] == pytest.approx(2_500.0 / 1_000.0)
    assert qtys[1] == pytest.approx(2_500.0 / 1_000.0)
    assert qtys[2] == pytest.approx(1_000.0 / 1_000.0)


def test_below_min_notional_skips_orders(planner_factory):
    positions = [
        {"venue": "binance", "symbol": "BTCUSDT", "base_qty": 0.001, "avg_price": 30_000.0}
    ]
    balances = [
        {"venue": "binance", "asset": "USDT", "qty": 1_000.0},
    ]
    planner = planner_factory(
        pos=positions,
        bal=balances,
        min_notional=200.0,
        max_notional_per_order=5_000.0,
    )
    residuals = [
        {
            "venue": "BINANCE",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "qty": 0.001,
            "strategy": "micro",
            "notional_usdt": 30.0,
            "funding_apr": 0.0,
            "taker_fee_bps": 2.0,
            "maker_fee_bps": 0.5,
        }
    ]

    orders = planner.plan(residuals)
    assert orders == []
    assert planner.last_plan_details["totals"]["orders"] == 0
