import time

import pytest

from app.risk.freeze import FreezeRule, get_freeze_registry, reset_freeze_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_freeze_registry()
    yield
    reset_freeze_registry()


def test_apply_and_match_scopes() -> None:
    registry = get_freeze_registry()

    assert registry.is_frozen() is False

    ts = time.time()
    registry.apply(FreezeRule(reason="RECON_CRITICAL::venue=binance", scope="venue", ts=ts))
    assert registry.is_frozen(venue="binance") is True
    assert registry.is_frozen(venue="binance-um") is True
    assert registry.is_frozen(venue="okx") is False

    registry.clear()

    registry.apply(
        FreezeRule(
            reason="RECON_CRITICAL::venue=binance::symbol=BTCUSDT",
            scope="symbol",
            ts=ts + 1,
        )
    )
    assert registry.is_frozen(venue="binance-um", symbol="BTCUSDT") is True
    assert registry.is_frozen(venue="binance-um", symbol="ETHUSDT") is False

    registry.clear()

    registry.apply(FreezeRule(reason="HEALTH_CRITICAL::BINANCE", scope="venue", ts=ts + 2))
    assert registry.is_frozen(venue="binance-futures") is True


def test_clear_by_prefix() -> None:
    registry = get_freeze_registry()
    now = time.time()
    registry.apply(FreezeRule(reason="RECON_CRITICAL::venue=okx", scope="venue", ts=now))
    registry.apply(FreezeRule(reason="HEALTH_CRITICAL::OKX", scope="venue", ts=now))
    assert registry.is_frozen(venue="okx") is True
    cleared = registry.clear("RECON_CRITICAL")
    assert cleared == 1
    assert registry.is_frozen(venue="okx") is True
    registry.clear("HEALTH_CRITICAL::")
    assert registry.is_frozen(venue="okx") is False
