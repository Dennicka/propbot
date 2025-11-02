from __future__ import annotations

import pytest

from app.config.schema import (
    AppConfig,
    ExposureCapsConfig,
    ExposureCapsEntry,
    ExposureSideCapsConfig,
)
from app.risk.exposure_caps import (
    ExposureCapsSnapshot,
    check_open_allowed,
    resolve_caps,
)


@pytest.fixture
def exposure_config() -> AppConfig:
    caps = ExposureCapsConfig(
        default=ExposureCapsEntry(
            max_abs_usdt=1000,
            per_side_max_abs_usdt=ExposureSideCapsConfig(LONG=600, SHORT=500),
        ),
        per_symbol={
            "ETHUSDT": ExposureCapsEntry(
                max_abs_usdt=2000,
                per_side_max_abs_usdt=ExposureSideCapsConfig(LONG=1500, SHORT=1200),
            )
        },
        per_venue={
            "okx": {
                "ETHUSDT": ExposureCapsEntry(max_abs_usdt=1200),
            }
        },
    )
    return AppConfig(profile="unit-test", exposure_caps=caps)


def test_resolve_caps_precedence(exposure_config: AppConfig) -> None:
    caps = resolve_caps(exposure_config, "ETHUSDT", "LONG", "okx")
    assert caps["global_max_abs"] == 2000
    assert caps["side_max_abs"] == 1500
    assert caps["venue_max_abs"] == 1200

    default_caps = resolve_caps(exposure_config, "BTCUSDT", "SHORT", "binance-um")
    assert default_caps["global_max_abs"] == 1000
    assert default_caps["side_max_abs"] == 500
    assert default_caps["venue_max_abs"] is None


def _snapshot(long_abs: float = 0.0, short_abs: float = 0.0) -> ExposureCapsSnapshot:
    by_symbol = {"ETHUSDT": long_abs + short_abs}
    by_symbol_side = {
        ("ETHUSDT", "LONG"): long_abs,
        ("ETHUSDT", "SHORT"): short_abs,
    }
    by_venue_symbol = {
        ("okx", "ETHUSDT"): {
            "symbol": "ETHUSDT",
            "venue": "okx",
            "base_qty": long_abs / 1000.0 if long_abs else -short_abs / 1000.0,
            "avg_price": 1000.0,
            "LONG": long_abs,
            "SHORT": short_abs,
            "total_abs": long_abs + short_abs,
            "side": "LONG" if long_abs else ("SHORT" if short_abs else "FLAT"),
        }
    }
    return ExposureCapsSnapshot(
        by_symbol=by_symbol,
        by_symbol_side=by_symbol_side,
        by_venue_symbol=by_venue_symbol,
    )


@pytest.mark.parametrize(
    "new_abs, expected",
    [
        (2100.0, "EXPOSURE_CAPS::GLOBAL"),
        (1600.0, "EXPOSURE_CAPS::SIDE"),
        (1300.0, "EXPOSURE_CAPS::VENUE"),
    ],
)
def test_block_opening_when_exceeds_global_or_side_or_venue(
    exposure_config: AppConfig, new_abs: float, expected: str
) -> None:
    snapshot = _snapshot()
    ctx: dict[str, object] = {"config": exposure_config, "snapshot": snapshot}
    allowed, reason = check_open_allowed(ctx, "ETHUSDT", "LONG", "okx", new_abs)
    assert not allowed
    assert reason == expected


def test_allow_reduce_only_when_exceeded(exposure_config: AppConfig) -> None:
    snapshot = _snapshot(long_abs=2100.0)
    ctx: dict[str, object] = {"config": exposure_config, "snapshot": snapshot}
    allowed, reason = check_open_allowed(ctx, "ETHUSDT", "LONG", "okx", 1800.0)
    assert allowed
    assert reason is None
