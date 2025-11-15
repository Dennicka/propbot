from decimal import Decimal

from app.pnl.models import PositionPnlSnapshot, build_strategy_pnl_snapshots


def test_build_strategy_pnl_snapshots_aggregates_two_positions_same_strategy() -> None:
    positions = [
        PositionPnlSnapshot(
            symbol="BTCUSDT",
            venue="binance",
            strategy_id="strat_a",
            realized_pnl=Decimal("10"),
            unrealized_pnl=Decimal("-2"),
            fees_paid=Decimal("-1"),
            funding_paid=Decimal("0.5"),
            notional_usd=Decimal("1000"),
        ),
        PositionPnlSnapshot(
            symbol="ETHUSDT",
            venue="binance",
            strategy_id="strat_a",
            realized_pnl=Decimal("5"),
            unrealized_pnl=Decimal("3"),
            fees_paid=Decimal("-0.2"),
            funding_paid=Decimal("0.1"),
            notional_usd=Decimal("500"),
        ),
    ]

    snapshots = build_strategy_pnl_snapshots(positions)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.strategy_id == "strat_a"
    assert snapshot.realized_pnl == Decimal("15")
    assert snapshot.unrealized_pnl == Decimal("1")
    assert snapshot.fees_paid == Decimal("-1.2")
    assert snapshot.funding_paid == Decimal("0.6")
    assert snapshot.gross_pnl == Decimal("16")
    assert snapshot.net_pnl == Decimal("15.4")
    assert snapshot.positions_count == 2
    assert snapshot.notional_usd == Decimal("1500")


def test_build_strategy_pnl_snapshots_skips_none_strategy_id() -> None:
    positions = [
        PositionPnlSnapshot(
            symbol="BTCUSDT",
            venue="binance",
            strategy_id=None,
            realized_pnl=Decimal("1"),
            unrealized_pnl=Decimal("1"),
        )
    ]

    snapshots = build_strategy_pnl_snapshots(positions)

    assert snapshots == []
