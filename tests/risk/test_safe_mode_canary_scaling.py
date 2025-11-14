import pytest

from app.risk.limits import apply_safe_mode_scaling


def _base_snapshot() -> dict[str, object]:
    return {
        "enabled": True,
        "max_notional_per_venue": {"binance": 10_000.0},
        "max_notional_per_symbol": {"binance:BTCUSDT": 10_000.0},
        "daily_loss_limit": 1_000.0,
        "daily_loss_used": None,
        "rejects_recent": None,
        "extra": {},
    }


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CANARY_MODE",
        "SAFE_MODE_GLOBAL",
        "SAFE_MODE_SCALE_NOTIONAL",
        "SAFE_MODE_SCALE_DAILY_LOSS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_scaling_disabled_when_flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAFE_MODE_SCALE_NOTIONAL", "0.1")
    monkeypatch.setenv("SAFE_MODE_SCALE_DAILY_LOSS", "0.25")

    base = _base_snapshot()
    scaled = apply_safe_mode_scaling(base, is_canary=False, safe_mode_global=False)

    assert scaled["max_notional_per_venue"]["binance"] == pytest.approx(10_000.0)
    assert scaled["max_notional_per_symbol"]["binance:BTCUSDT"] == pytest.approx(10_000.0)
    assert scaled["daily_loss_limit"] == pytest.approx(1_000.0)


def test_canary_mode_scales_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAFE_MODE_SCALE_NOTIONAL", "0.1")
    monkeypatch.setenv("SAFE_MODE_SCALE_DAILY_LOSS", "0.25")

    base = _base_snapshot()
    scaled = apply_safe_mode_scaling(base, is_canary=True, safe_mode_global=False)

    assert scaled["max_notional_per_venue"]["binance"] == pytest.approx(1_000.0)
    assert scaled["max_notional_per_symbol"]["binance:BTCUSDT"] == pytest.approx(1_000.0)
    assert scaled["daily_loss_limit"] == pytest.approx(250.0)


def test_safe_mode_global_scales_without_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAFE_MODE_SCALE_NOTIONAL", "0.1")
    monkeypatch.setenv("SAFE_MODE_SCALE_DAILY_LOSS", "0.25")

    base = _base_snapshot()
    scaled = apply_safe_mode_scaling(base, is_canary=False, safe_mode_global=True)

    assert scaled["max_notional_per_venue"]["binance"] == pytest.approx(1_000.0)
    assert scaled["max_notional_per_symbol"]["binance:BTCUSDT"] == pytest.approx(1_000.0)
    assert scaled["daily_loss_limit"] == pytest.approx(250.0)
