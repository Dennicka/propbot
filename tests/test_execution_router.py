import types

import pytest

from services import execution_router


class DummyClient:
    def __init__(self, mark_price: float, available: float) -> None:
        self.mark_price = mark_price
        self.available = available

    def get_mark_price(self, symbol: str) -> dict:
        return {"symbol": symbol, "mark_price": float(self.mark_price)}

    def get_account_limits(self) -> dict:
        return {"available_balance": float(self.available)}


@pytest.fixture(autouse=True)
def _patch_runtime(monkeypatch):
    state = types.SimpleNamespace(
        control=types.SimpleNamespace(
            taker_fee_bps_binance=2,
            taker_fee_bps_okx=5,
        )
    )
    monkeypatch.setattr(execution_router, "get_state", lambda: state)
    monkeypatch.setattr(execution_router, "is_dry_run_mode", lambda: False)


def test_choose_venue_prefers_best_effective_price(monkeypatch):
    binance = DummyClient(mark_price=100.0, available=10_000.0)
    okx = DummyClient(mark_price=99.4, available=10_000.0)
    monkeypatch.setattr(
        execution_router,
        "_CLIENTS",
        {
            "binance": execution_router._VenueAdapter("binance", binance),
            "okx": execution_router._VenueAdapter("okx", okx),
        },
    )

    result = execution_router.choose_venue("long", "BTCUSDT", 1.0)

    assert result["venue"] == "okx"
    assert pytest.approx(result["expected_fill_px"]) == 99.4
    assert result["liquidity_ok"] is True


def test_choose_venue_marks_insufficient_liquidity(monkeypatch):
    binance = DummyClient(mark_price=100.0, available=500.0)
    okx = DummyClient(mark_price=101.0, available=5.0)
    monkeypatch.setattr(
        execution_router,
        "_CLIENTS",
        {
            "binance": execution_router._VenueAdapter("binance", binance),
            "okx": execution_router._VenueAdapter("okx", okx),
        },
    )

    result = execution_router.choose_venue("short", "ETHUSDT", 2.0)

    assert result["venue"] == "binance"
    assert result["liquidity_ok"] is True

    result_large = execution_router.choose_venue("short", "ETHUSDT", 100.0)
    assert result_large["liquidity_ok"] is False
