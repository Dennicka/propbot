import pytest

from app.services.positions_view import build_positions_snapshot


class _StubState:
    derivatives = None


@pytest.mark.asyncio
async def test_portfolio_exposure_aggregates_positions_correctly() -> None:
    positions = [
        {
            "id": "btc-long-short",
            "symbol": "BTCUSDT",
            "status": "open",
            "legs": [
                {
                    "venue": "binance",
                    "symbol": "BTCUSDT",
                    "side": "long",
                    "notional_usdt": 1_200.0,
                    "entry_price": 24_000.0,
                    "base_size": 0.05,
                },
                {
                    "venue": "binance",
                    "symbol": "BTCUSDT",
                    "side": "short",
                    "notional_usdt": 300.0,
                    "entry_price": 25_000.0,
                    "base_size": 0.012,
                },
            ],
        },
        {
            "id": "eth-short-heavy",
            "symbol": "ETHUSDT",
            "status": "partial",
            "legs": [
                {
                    "venue": "okx",
                    "symbol": "ETHUSDT",
                    "side": "long",
                    "notional_usdt": 500.0,
                    "entry_price": 1_500.0,
                    "base_size": 0.3333333333,
                },
                {
                    "venue": "okx",
                    "symbol": "ETHUSDT",
                    "side": "short",
                    "notional_usdt": 900.0,
                    "entry_price": 1_520.0,
                    "base_size": 0.5921052631,
                },
            ],
        },
    ]

    snapshot = await build_positions_snapshot(_StubState(), positions)

    assert "exposure" in snapshot
    per_venue = snapshot["exposure"]
    assert per_venue["binance"]["long_notional"] == pytest.approx(1_200.0)
    assert per_venue["binance"]["short_notional"] == pytest.approx(300.0)
    assert per_venue["binance"]["net_usdt"] == pytest.approx(900.0)

    assert per_venue["okx"]["long_notional"] == pytest.approx(500.0)
    assert per_venue["okx"]["short_notional"] == pytest.approx(900.0)
    assert per_venue["okx"]["net_usdt"] == pytest.approx(-400.0)

    rendered_positions = snapshot["positions"]
    assert len(rendered_positions) == 2
    first_position = rendered_positions[0]
    assert first_position["symbol"] == "BTCUSDT"
    first_leg = first_position["legs"][0]
    assert first_leg["side"] == "long"
    assert first_leg["notional_usdt"] == pytest.approx(1_200.0)
    assert first_leg["base_size"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_exposure_handles_empty_positions() -> None:
    snapshot = await build_positions_snapshot(_StubState(), [])

    assert snapshot["positions"] == []
    assert snapshot["exposure"] == {}
    assert snapshot["totals"]["unrealized_pnl_usdt"] == pytest.approx(0.0)
