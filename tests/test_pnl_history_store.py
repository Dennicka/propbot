import pytest

from app import ledger
from app.services import runtime
from app.services.pnl_history import record_snapshot
from positions import create_position, reset_positions
from pnl_history_store import list_recent


@pytest.mark.asyncio
async def test_pnl_history_store_filters_simulated_exposure(monkeypatch) -> None:
    runtime.reset_for_tests()
    ledger.reset()
    reset_positions()

    class DummySnapshot:
        pnl_totals = {"unrealized": 42.0, "total": 42.0, "realized": 0.0}

    async def fake_snapshot(*_args, **_kwargs):
        return DummySnapshot()

    monkeypatch.setattr("app.services.pnl_history.portfolio.snapshot", fake_snapshot)

    create_position(
        symbol="ETHUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=1000.0,
        entry_spread_bps=12.0,
        leverage=2.0,
        entry_long_price=1800.0,
        entry_short_price=1805.0,
        status="open",
        legs=[
            {
                "side": "long",
                "venue": "binance-um",
                "symbol": "ETHUSDT",
                "notional_usdt": 1000.0,
                "entry_price": 1800.0,
                "status": "open",
            },
            {
                "side": "short",
                "venue": "okx-perp",
                "symbol": "ETHUSDT",
                "notional_usdt": 1000.0,
                "entry_price": 1805.0,
                "status": "open",
            },
        ],
    )

    create_position(
        symbol="BTCUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=800.0,
        entry_spread_bps=5.0,
        leverage=1.5,
        entry_long_price=28000.0,
        entry_short_price=28005.0,
        status="partial",
        legs=[
            {
                "side": "long",
                "venue": "binance-um",
                "symbol": "BTCUSDT",
                "notional_usdt": 600.0,
                "entry_price": 28000.0,
                "status": "partial",
            },
            {
                "side": "short",
                "venue": "okx-perp",
                "symbol": "BTCUSDT",
                "notional_usdt": 300.0,
                "entry_price": 28005.0,
                "status": "partial",
            },
        ],
    )

    create_position(
        symbol="LTCUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=400.0,
        entry_spread_bps=3.0,
        leverage=1.0,
        entry_long_price=90.0,
        entry_short_price=90.5,
        status="open",
        simulated=True,
        legs=[
            {
                "side": "long",
                "venue": "binance-um",
                "symbol": "LTCUSDT",
                "notional_usdt": 400.0,
                "entry_price": 90.0,
                "status": "simulated",
            },
            {
                "side": "short",
                "venue": "okx-perp",
                "symbol": "LTCUSDT",
                "notional_usdt": 400.0,
                "entry_price": 90.5,
                "status": "simulated",
            },
        ],
    )

    await record_snapshot(reason="test", max_entries=10)

    create_position(
        symbol="XRPUSDT",
        long_venue="okx-perp",
        short_venue="binance-um",
        notional_usdt=500.0,
        entry_spread_bps=4.0,
        leverage=1.2,
        entry_long_price=0.5,
        entry_short_price=0.51,
        status="open",
        legs=[
            {
                "side": "long",
                "venue": "okx-perp",
                "symbol": "XRPUSDT",
                "notional_usdt": 500.0,
                "entry_price": 0.5,
                "status": "open",
            },
            {
                "side": "short",
                "venue": "binance-um",
                "symbol": "XRPUSDT",
                "notional_usdt": 500.0,
                "entry_price": 0.51,
                "status": "open",
            },
        ],
    )

    await record_snapshot(reason="test", max_entries=10)

    latest_only = list_recent(limit=1)
    assert len(latest_only) == 1

    recent = list_recent(limit=2)
    assert len(recent) == 2
    latest = recent[0]

    assert latest["open_positions"] == 2
    assert latest["partial_positions"] == 1
    assert latest.get("open_positions_total") == 3
    assert latest.get("simulated", {}).get("positions") == 1

    total_exposure = latest["total_exposure_usd_total"]
    # Real legs: ETH 1000+1000, BTC 600+300, XRP 500+500 => 3900
    assert total_exposure == pytest.approx(3900.0)

    per_venue = latest["total_exposure_usd"]
    assert per_venue["binance-um"] == pytest.approx(2100.0)
    assert per_venue["okx-perp"] == pytest.approx(1800.0)

    simulated_payload = latest.get("simulated", {})
    assert simulated_payload.get("total") == pytest.approx(800.0)
    assert simulated_payload.get("per_venue", {}).get("binance-um") == pytest.approx(400.0)
    assert simulated_payload.get("per_venue", {}).get("okx-perp") == pytest.approx(400.0)

    # Ensure the older snapshot stayed below the max_entries limit
    older = recent[1]
    assert older["total_exposure_usd_total"] < total_exposure
