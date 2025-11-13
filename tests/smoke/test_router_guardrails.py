from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.config.profile import TradingProfile
from app.router import smart_router
from app.services.safe_mode import SafeMode


def _enable_test_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ONLY_ROUTER_META", "*")
    monkeypatch.setenv("TEST_ONLY_ROUTER_TICK_SIZE", "0.1")
    monkeypatch.setenv("TEST_ONLY_ROUTER_STEP_SIZE", "0.001")
    monkeypatch.setenv("TEST_ONLY_ROUTER_MIN_QTY", "0.0001")
    monkeypatch.delenv("TEST_ONLY_ROUTER_MIN_NOTIONAL", raising=False)
    monkeypatch.setenv("FF_MD_WATCHDOG", "0")


@pytest.fixture()
def router_factory(monkeypatch: pytest.MonkeyPatch):
    SafeMode.set(False)
    profile = TradingProfile(name="paper", allow_trading=True, strict_flags=False)
    state = SimpleNamespace(
        control=SimpleNamespace(
            post_only=False,
            taker_fee_bps_binance=2,
            taker_fee_bps_okx=2,
            default_taker_fee_bps=2,
        ),
        config=SimpleNamespace(
            data=SimpleNamespace(
                tca=SimpleNamespace(
                    horizon_min=1.0,
                    impact=SimpleNamespace(k=0.0),
                    tiers={},
                ),
                derivatives=SimpleNamespace(
                    arbitrage=SimpleNamespace(prefer_maker=False),
                    fees=SimpleNamespace(manual={}),
                ),
            )
        ),
        derivatives=SimpleNamespace(venues={}),
    )
    monkeypatch.setattr(smart_router, "get_state", lambda: state)
    monkeypatch.setattr(smart_router, "get_market_data", lambda: SimpleNamespace())
    monkeypatch.setattr(smart_router, "get_liquidity_status", lambda: {})
    monkeypatch.setattr(smart_router, "get_profile", lambda: profile)
    monkeypatch.setattr(smart_router.provider, "get", lambda *_, **__: None)

    def factory() -> smart_router.SmartRouter:
        return smart_router.SmartRouter()

    yield factory
    SafeMode.set(False)


def _register(router: smart_router.SmartRouter, **overrides: object) -> dict[str, object]:
    intent = {
        "strategy": "smoke",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "side": "buy",
        "qty": 0.001,
        "price": 50000.0,
        "ts_ns": 1,
        "nonce": 1,
    }
    intent.update(overrides)
    return router.register_order(**intent)


def test_safe_mode_blocks_submission(monkeypatch: pytest.MonkeyPatch, router_factory) -> None:
    _enable_test_metadata(monkeypatch)
    monkeypatch.setenv("SAFE_MODE", "1")
    monkeypatch.setenv("FF_RISK_LIMITS", "0")
    monkeypatch.setenv("FF_PRETRADE_STRICT", "0")
    SafeMode.set(True)
    router = router_factory()

    response = _register(router)

    assert response == {"ok": False, "reason": "safe-mode"}


def test_pretrade_strict_rejects_zero_qty(monkeypatch: pytest.MonkeyPatch, router_factory) -> None:
    _enable_test_metadata(monkeypatch)
    monkeypatch.setenv("FF_PRETRADE_STRICT", "1")
    monkeypatch.setenv("FF_RISK_LIMITS", "0")
    SafeMode.set(False)
    router = router_factory()

    response = _register(router, qty=0.0)

    assert response.get("status") == "pretrade_rejected"
    assert response.get("reason") == "qty_invalid"


def test_risk_limits_block_when_enabled(monkeypatch: pytest.MonkeyPatch, router_factory) -> None:
    _enable_test_metadata(monkeypatch)
    monkeypatch.setenv("FF_RISK_LIMITS", "1")
    monkeypatch.setenv("RISK_CAP_SYMBOL", "binance:BTCUSDT:0")
    SafeMode.set(False)
    router = router_factory()

    response = _register(router, qty=1.0, price=Decimal("25000"))

    assert response.get("status", "").startswith("risk-blocked:")
    assert response.get("reason") == "symbol_cap"
