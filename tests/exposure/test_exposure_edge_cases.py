import pytest

from app.services.positions_view import build_positions_snapshot


class _StubState:
    derivatives = None


@pytest.mark.asyncio
async def test_portfolio_exposure_net_flat_for_equal_long_short() -> None:
    positions = [
        {
            "id": "btc-flat",
            "symbol": "BTCUSDT",
            "status": "open",
            "legs": [
                {
                    "venue": "binance",
                    "symbol": "BTCUSDT",
                    "side": "long",
                    "notional_usdt": 1000.0,
                    "entry_price": 20_000.0,
                    "base_size": 0.05,
                },
                {
                    "venue": "binance",
                    "symbol": "BTCUSDT",
                    "side": "short",
                    "notional_usdt": 1000.0,
                    "entry_price": 20_000.0,
                    "base_size": 0.05,
                },
            ],
        }
    ]

    snapshot = await build_positions_snapshot(_StubState(), positions)

    venue_exposure = snapshot["exposure"].get("binance")
    assert venue_exposure is not None
    assert venue_exposure["long_notional"] == pytest.approx(1000.0)
    assert venue_exposure["short_notional"] == pytest.approx(1000.0)
    assert venue_exposure["net_usdt"] == pytest.approx(0.0, abs=1e-9)


@pytest.mark.asyncio
async def test_portfolio_exposure_empty_positions_zero() -> None:
    snapshot = await build_positions_snapshot(_StubState(), [])

    assert snapshot["positions"] == []
    assert snapshot["exposure"] == {}
    assert snapshot["totals"]["unrealized_pnl_usdt"] == pytest.approx(0.0)
