from decimal import Decimal

from app.pnl.models import PortfolioPnlSnapshot, PositionPnlSnapshot, aggregate_portfolio_pnl


def test_portfolio_pnl_zero_positions_is_zero() -> None:
    snapshot = aggregate_portfolio_pnl(())

    assert snapshot.realized_pnl == Decimal("0")
    assert snapshot.unrealized_pnl == Decimal("0")
    assert snapshot.fees_paid == Decimal("0")
    assert snapshot.funding_paid == Decimal("0")
    assert snapshot.gross_pnl == Decimal("0")
    assert snapshot.net_pnl == Decimal("0")
    assert snapshot.positions == ()


def test_portfolio_pnl_with_positive_fees_decreases_net_pnl() -> None:
    fees_amount = Decimal("2")
    positions = (
        PositionPnlSnapshot(
            symbol="BTCUSDT",
            venue="binance",
            realized_pnl=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            fees_paid=-fees_amount,
            funding_paid=Decimal("0"),
        ),
    )

    snapshot = aggregate_portfolio_pnl(positions)

    assert snapshot.gross_pnl == Decimal("10")
    assert snapshot.fees_paid == -fees_amount
    assert snapshot.funding_paid == Decimal("0")
    assert snapshot.net_pnl == Decimal("8")


def test_portfolio_pnl_with_negative_pnl_and_fees() -> None:
    fees_amount = Decimal("1")
    positions = (
        PositionPnlSnapshot(
            symbol="ETHUSDT",
            venue="binance",
            realized_pnl=Decimal("-5"),
            unrealized_pnl=Decimal("0"),
            fees_paid=-fees_amount,
            funding_paid=Decimal("0"),
        ),
    )

    snapshot = aggregate_portfolio_pnl(positions)

    assert snapshot.realized_pnl == Decimal("-5")
    assert snapshot.gross_pnl == Decimal("-5")
    assert snapshot.fees_paid == -fees_amount
    assert snapshot.net_pnl == Decimal("-6")


def test_portfolio_pnl_handles_missing_funding_as_zero() -> None:
    snapshot = PortfolioPnlSnapshot(
        realized_pnl=Decimal("3"),
        unrealized_pnl=Decimal("2"),
        fees_paid=Decimal("-1"),
        funding_paid=None,
    )

    assert snapshot.funding_paid == Decimal("0")
    assert snapshot.net_pnl == Decimal("4")
