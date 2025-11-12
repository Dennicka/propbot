from decimal import Decimal

import pytest

from app.exchanges.metadata import SymbolMeta, provider
from app.router.smart_router import SmartRouter


class DummyState:
    config = None


@pytest.fixture(autouse=True)
def reset_metadata_provider() -> None:
    provider.clear()
    yield
    provider.clear()


@pytest.fixture(autouse=True)
def enable_strict_pretrade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FF_PRETRADE_STRICT", "1")
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})


@pytest.fixture
def router() -> SmartRouter:
    return SmartRouter(state=DummyState(), market_data={})


def test_register_order_rejects_on_qty_step_violation(router: SmartRouter) -> None:
    provider.put(
        "binance",
        "BTCUSDT",
        SymbolMeta(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_notional=Decimal("5"),
            min_qty=Decimal("0.001"),
        ),
    )

    response = router.register_order(
        strategy="arb",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=0.0015,
        price=20000.0,
        ts_ns=1,
        nonce=1,
    )

    assert response["status"] == "pretrade_rejected"
    assert response["reason"] == "qty_step"


def test_register_order_rejects_on_minimums(router: SmartRouter) -> None:
    provider.put(
        "okx",
        "BTC-USDT-SWAP",
        SymbolMeta(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_notional=Decimal("10"),
            min_qty=Decimal("0.01"),
        ),
    )

    response = router.register_order(
        strategy="arb",
        venue="okx",
        symbol="BTC-USDT-SWAP",
        side="buy",
        qty=0.005,
        price=500.0,
        ts_ns=2,
        nonce=1,
    )

    assert response["status"] == "pretrade_rejected"
    assert response["reason"] in {"min_qty", "min_notional"}


def test_register_order_rejects_without_metadata(router: SmartRouter) -> None:
    response = router.register_order(
        strategy="arb",
        venue="bybit",
        symbol="BTCUSDT",
        side="sell",
        qty=1.0,
        price=20000.0,
        ts_ns=3,
        nonce=1,
    )

    assert response["status"] == "pretrade_rejected"
    assert response["reason"] == "no_meta"
