import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.config import feature_flags as ff
from app.exchanges.metadata import SymbolMeta, provider
from app.orders.idempotency import IdempoStore
from app.orders.state import OrderState
from app.router.smart_router import SmartRouter


class TrackingIdempoStore(IdempoStore):
    def __init__(self) -> None:
        super().__init__()
        self.should_send_calls = 0

    def should_send(self, coid: str) -> bool:
        self.should_send_calls += 1
        return super().should_send(coid)


@pytest.fixture(autouse=True)
def _clear_metadata() -> None:
    provider.clear()
    yield
    provider.clear()


def _seed_metadata() -> None:
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


def _make_router(monkeypatch: pytest.MonkeyPatch, idempo: IdempoStore) -> SmartRouter:
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    state = SimpleNamespace(
        config=SimpleNamespace(data=None), derivatives=SimpleNamespace(venues={})
    )
    market_data = SimpleNamespace()
    return SmartRouter(state=state, market_data=market_data, idempo_store=idempo)


def _set_default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_PROFILE", "paper")
    monkeypatch.delenv("FF_PRETRADE_STRICT", raising=False)
    monkeypatch.delenv("FF_RISK_LIMITS", raising=False)
    monkeypatch.setenv("RISK_CAP_SYMBOL", "")
    monkeypatch.setenv("RISK_CAP_STRATEGY", "")


def test_pretrade_strict_rejects_invalid_qty(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default_env(monkeypatch)
    monkeypatch.setenv("RISK_CAP_VENUE", "binance:100000")
    tracker = TrackingIdempoStore()
    router = _make_router(monkeypatch, tracker)
    _seed_metadata()

    assert ff.pretrade_strict_on() is True

    response = router.register_order(
        strategy="alpha",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=Decimal("-0.01"),
        price=Decimal("30000"),
        ts_ns=1,
        nonce=1,
    )

    assert response["status"] == "pretrade_rejected"
    assert response["reason"] == "qty_invalid"
    assert tracker.should_send_calls == 1


def test_risk_limits_block_high_notional(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _set_default_env(monkeypatch)
    monkeypatch.setenv("RISK_CAP_VENUE", "binance:1000")
    tracker = TrackingIdempoStore()
    router = _make_router(monkeypatch, tracker)
    _seed_metadata()

    assert ff.risk_limits_on() is True

    caplog.set_level(logging.WARNING, logger="app.router.smart_router")

    response = router.register_order(
        strategy="alpha",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=Decimal("0.05"),
        price=Decimal("35000"),
        ts_ns=2,
        nonce=2,
    )

    assert response["status"] == "risk-blocked:venue_cap"
    assert response["reason"] == "venue_cap"
    assert tracker.should_send_calls == 0
    assert any(
        getattr(record, "details", {}).get("reason") == "venue_cap" for record in caplog.records
    )


def test_order_passes_within_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default_env(monkeypatch)
    monkeypatch.setenv("RISK_CAP_VENUE", "binance:1000")
    tracker = TrackingIdempoStore()
    router = _make_router(monkeypatch, tracker)
    _seed_metadata()

    response = router.register_order(
        strategy="alpha",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=Decimal("0.02"),
        price=Decimal("25000"),
        ts_ns=3,
        nonce=3,
    )

    assert "status" not in response or response["status"] != "pretrade_rejected"
    assert response["state"] == OrderState.PENDING
    assert tracker.should_send_calls == 1
