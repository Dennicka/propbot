from decimal import Decimal

from app.pnl.ledger import FundingEvent, PnLLedger, TradeFill


def test_apply_fill_buy_sell_realized() -> None:
    ledger = PnLLedger()
    buy = TradeFill(
        venue="binance",
        symbol="BTCUSDT",
        side="BUY",
        qty=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0.1"),
        fee_asset="USDT",
        ts=1.0,
    )
    sell = TradeFill(
        venue="binance",
        symbol="BTCUSDT",
        side="SELL",
        qty=Decimal("1"),
        price=Decimal("110"),
        fee=Decimal("0.1"),
        fee_asset="USDT",
        ts=2.0,
    )
    ledger.apply_fill(buy, exclude_simulated=False)
    ledger.apply_fill(sell, exclude_simulated=False)

    entries = list(ledger.iter_entries())
    assert len(entries) == 2
    assert entries[-1].realized_pnl == Decimal("10")

    snapshot = ledger.get_snapshot()
    totals = snapshot["totals"]
    assert totals["realized_pnl"] == Decimal("10")
    assert totals["fees"] == Decimal("0.2")
    assert totals["net_pnl"] == Decimal("9.8")


def test_exclude_simulated_fills_respected() -> None:
    ledger = PnLLedger()
    fill = TradeFill(
        venue="binance",
        symbol="ETHUSDT",
        side="BUY",
        qty=Decimal("2"),
        price=Decimal("50"),
        fee=Decimal("0"),
        fee_asset="USDT",
        ts=3.0,
        is_simulated=True,
    )
    ledger.apply_fill(fill, exclude_simulated=True)

    snapshot = ledger.get_snapshot()
    assert snapshot["totals"]["realized_pnl"] == Decimal("0")
    assert not list(ledger.iter_entries())


def test_apply_funding_accumulates_correctly() -> None:
    ledger = PnLLedger()
    event = FundingEvent(
        venue="okx",
        symbol="BTCUSDT",
        amount=Decimal("5.5"),
        asset="USDT",
        ts=4.0,
    )
    ledger.apply_funding(event)

    entries = list(ledger.iter_entries())
    assert len(entries) == 1
    assert entries[0].funding == Decimal("5.5")

    snapshot = ledger.get_snapshot()
    totals = snapshot["totals"]
    assert totals["funding"] == Decimal("5.5")
    assert totals["net_pnl"] == Decimal("5.5")
