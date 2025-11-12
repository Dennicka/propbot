from datetime import datetime, time as dt_time, timezone
from decimal import Decimal

import pytest
from zoneinfo import ZoneInfo

from app.rules.pretrade import PretradeRejection, SymbolSpecs, TimeWindow, validate_pretrade


_DEF_NOW = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_validate_pretrade_blocks_on_symbol_halt():
    meta = SymbolSpecs(
        symbol="BTCUSDT",
        tick=0.1,
        lot=0.01,
        min_notional=10.0,
        blocked=True,
        reason="halted",
    )

    with pytest.raises(PretradeRejection) as excinfo:
        validate_pretrade("buy", Decimal("100"), Decimal("1"), meta, now=_DEF_NOW)

    assert excinfo.value.reason == "halted"


def test_validate_pretrade_rejects_outside_trade_window():
    window = TimeWindow(start=dt_time(13, 0), end=dt_time(14, 0), tz=ZoneInfo("UTC"))
    meta = SymbolSpecs(
        symbol="BTCUSDT",
        tick=0.1,
        lot=0.01,
        min_notional=10.0,
        trade_hours=[window],
    )

    with pytest.raises(PretradeRejection) as excinfo:
        validate_pretrade("sell", Decimal("100"), Decimal("1"), meta, now=_DEF_NOW)

    assert excinfo.value.reason == "outside_trade_hours"


def test_validate_pretrade_allows_when_within_window_and_notional_ok():
    window = TimeWindow(start=dt_time(11, 0), end=dt_time(13, 30), tz=ZoneInfo("UTC"))
    meta = SymbolSpecs(
        symbol="BTCUSDT",
        tick=0.1,
        lot=0.01,
        min_notional=Decimal("50"),
        trade_hours=[window],
    )

    validate_pretrade("buy", Decimal("200"), Decimal("1"), meta, now=_DEF_NOW)


def test_validate_pretrade_blocks_min_notional():
    meta = SymbolSpecs(
        symbol="BTCUSDT",
        tick=0.1,
        lot=0.01,
        min_notional=Decimal("500"),
    )

    with pytest.raises(PretradeRejection) as excinfo:
        validate_pretrade("buy", Decimal("200"), Decimal("1"), meta, now=_DEF_NOW)

    assert excinfo.value.reason == "min_notional"
