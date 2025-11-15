from decimal import Decimal

import pytest

from app.pnl.strategy_metrics import (
    TradeRecord,
    build_strategy_performance,
    compute_max_drawdown,
)


def test_build_strategy_performance_basic() -> None:
    trades = [
        TradeRecord(
            strategy_id="alpha",
            symbol="BTCUSDT",
            side="BUY",
            qty=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0"),
            ts=1.0,
        ),
        TradeRecord(
            strategy_id="alpha",
            symbol="BTCUSDT",
            side="SELL",
            qty=Decimal("1"),
            price=Decimal("110"),
            fee=Decimal("1"),
            ts=2.0,
        ),
    ]

    snapshots = build_strategy_performance(trades)
    assert len(snapshots) == 1

    snapshot = snapshots[0]
    assert snapshot.strategy_id == "alpha"
    assert snapshot.trades_count == 2
    assert snapshot.winning_trades == 1
    assert snapshot.losing_trades == 0
    assert snapshot.gross_pnl == Decimal("10")
    assert snapshot.net_pnl == Decimal("9")
    assert snapshot.average_trade_pnl == Decimal("4.5")
    assert snapshot.turnover_notional == Decimal("210")
    assert snapshot.max_drawdown == Decimal("0")
    assert snapshot.winrate == pytest.approx(0.5)


def test_build_strategy_performance_skips_unknown_strategy() -> None:
    trades = [
        TradeRecord(
            strategy_id=" ",
            symbol="BTCUSDT",
            side="BUY",
            qty=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0"),
            ts=1.0,
        )
    ]

    snapshots = build_strategy_performance(trades)
    assert snapshots == []


def test_compute_max_drawdown() -> None:
    curve = [
        Decimal("0"),
        Decimal("10"),
        Decimal("5"),
        Decimal("15"),
        Decimal("8"),
    ]
    assert compute_max_drawdown(curve) == Decimal("7")
