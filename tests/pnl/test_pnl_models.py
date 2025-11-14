from decimal import Decimal

from app.pnl.models import (
    PortfolioPnlSnapshot,
    PositionPnlSnapshot,
    aggregate_portfolio_pnl,
)


def test_position_pnl_gross_and_net_zero_fees() -> None:
    snapshot = PositionPnlSnapshot(
        symbol="BTCUSDT",
        venue="BINANCE",
        realized_pnl=Decimal("10"),
        unrealized_pnl=Decimal("-2"),
        fees_paid=Decimal("0"),
        funding_paid=Decimal("0"),
    )

    assert snapshot.gross_pnl == Decimal("8")
    assert snapshot.net_pnl == Decimal("8")


def test_position_pnl_net_includes_fees_and_funding() -> None:
    snapshot = PositionPnlSnapshot(
        symbol="ETHUSDT",
        venue="FTX",
        realized_pnl=Decimal("10"),
        unrealized_pnl=Decimal("0"),
        fees_paid=Decimal("-1"),
        funding_paid=Decimal("2"),
    )

    assert snapshot.gross_pnl == Decimal("10")
    assert snapshot.net_pnl == Decimal("11")


def test_aggregate_portfolio_pnl() -> None:
    positions = [
        PositionPnlSnapshot(
            symbol="BTCUSDT",
            venue="binance",
            realized_pnl=Decimal("4"),
            unrealized_pnl=Decimal("1"),
            fees_paid=Decimal("-0.5"),
            funding_paid=Decimal("0.2"),
        ),
        PositionPnlSnapshot(
            symbol="ETHUSDT",
            venue="binance",
            realized_pnl=Decimal("6"),
            unrealized_pnl=Decimal("-3"),
            fees_paid=Decimal("-0.3"),
            funding_paid=Decimal("0.1"),
        ),
    ]

    snapshot = aggregate_portfolio_pnl(positions)

    assert isinstance(snapshot, PortfolioPnlSnapshot)
    assert snapshot.realized_pnl == Decimal("10")
    assert snapshot.unrealized_pnl == Decimal("-2")
    assert snapshot.fees_paid == Decimal("-0.8")
    assert snapshot.funding_paid == Decimal("0.3")
    assert snapshot.gross_pnl == Decimal("8")
    assert snapshot.net_pnl == Decimal("7.5")
