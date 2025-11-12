from decimal import Decimal

from app.exchanges.metadata import (
    SymbolMeta,
    normalize_binance,
    normalize_bybit,
    normalize_okx,
)


def test_normalize_binance_payload() -> None:
    raw = {
        "symbol": "BTCUSDT",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {
                "filterType": "LOT_SIZE",
                "stepSize": "0.001",
                "minQty": "0.001",
            },
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }

    meta = normalize_binance(raw)

    assert meta == SymbolMeta(
        tick_size=Decimal("0.10"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
        min_qty=Decimal("0.001"),
    )


def test_normalize_okx_payload() -> None:
    raw = {
        "instId": "BTC-USDT-SWAP",
        "tickSz": "0.1",
        "lotSz": "0.001",
        "minSz": "0.002",
        "ctVal": "1",
    }

    meta = normalize_okx(raw)

    assert meta == SymbolMeta(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("0.002"),
        min_qty=Decimal("0.002"),
    )


def test_normalize_bybit_payload() -> None:
    raw = {
        "symbol": "BTCUSDT",
        "priceFilter": {"tickSize": "0.5"},
        "lotSizeFilter": {"qtyStep": "0.001", "minQty": "0.001"},
        "minNotional": "10",
    }

    meta = normalize_bybit(raw)

    assert meta == SymbolMeta(
        tick_size=Decimal("0.5"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("10"),
        min_qty=Decimal("0.001"),
    )
