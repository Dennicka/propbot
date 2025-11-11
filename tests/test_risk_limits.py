from decimal import Decimal

from app.config.trading_profiles import TradingProfile
from app.services.risk_limits import check_daily_loss, check_global_notional, check_symbol_notional


def _profile() -> TradingProfile:
    return TradingProfile(
        name="test",
        max_notional_per_order=Decimal("100"),
        max_notional_per_symbol=Decimal("500"),
        max_notional_global=Decimal("2000"),
        daily_loss_limit=Decimal("200"),
        env_tag="test",
        allow_new_orders=True,
        allow_closures_only=False,
    )


def test_symbol_notional_within_limit() -> None:
    profile = _profile()
    result = check_symbol_notional("BTCUSDT", Decimal("250"), profile)
    assert result.allowed is True
    assert result.limit == Decimal("500")


def test_symbol_notional_exceeds_limit() -> None:
    profile = _profile()
    result = check_symbol_notional("BTCUSDT", Decimal("600"), profile)
    assert result.allowed is False


def test_global_notional_limit() -> None:
    profile = _profile()
    assert check_global_notional(Decimal("1500"), profile).allowed is True
    assert check_global_notional(Decimal("2500"), profile).allowed is False


def test_daily_loss_guard() -> None:
    profile = _profile()
    ok = check_daily_loss(Decimal("150"), profile)
    breach = check_daily_loss(Decimal("250"), profile)
    assert ok.allowed is True
    assert breach.allowed is False
