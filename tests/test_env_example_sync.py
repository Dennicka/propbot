from __future__ import annotations

from pathlib import Path

import pytest

from app.risk.core import FeatureFlags, get_risk_governor, reset_risk_governor_for_tests
from app.strategy.pnl_tracker import StrategyPnlTracker
from app.services import exchange_watchdog_runner


REQUIRED_FLAGS = {
    "APP_ENV": "local",
    "DEFAULT_PROFILE": "paper",
    "API_HOST": "127.0.0.1",
    "API_PORT": "8000",
    "DRY_RUN_MODE": "false",
    "RISK_CHECKS_ENABLED": "false",
    "ENFORCE_MAX_OPEN_TRADES": "false",
    "MAX_OPEN_TRADES": "5",
    "ENFORCE_MAX_NOTIONAL_USDT": "false",
    "MAX_TOTAL_NOTIONAL_USDT": "75000",
    "DAILY_LOSS_CAP_ENABLED": "false",
    "DAILY_LOSS_USDT": "0",
    "WATCHDOG_ENABLED": "false",
    "WATCHDOG_INTERVAL_SEC": "7",
    "WATCHDOG_AUTO_HOLD": "false",
    "NOTIFY_WATCHDOG": "false",
    "EXCLUDE_DRY_RUN_FROM_PNL": "true",
    "AUTOPILOT_GUARD_ENABLED": "true",
    "PROMETHEUS_ENABLED": "true",
}


def _parse_env_example() -> dict[str, str]:
    entries: dict[str, str] = {}
    env_path = Path(__file__).resolve().parent.parent / ".env.example"
    raw = env_path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.split("#", 1)[0].strip()
        if not key:
            continue
        entries[key] = value.strip().strip('"')
    return entries


def test_env_example_declares_required_flags() -> None:
    values = _parse_env_example()
    missing = {flag for flag in REQUIRED_FLAGS if flag not in values}
    assert not missing, f"missing flags in .env.example: {sorted(missing)}"
    for flag, default in REQUIRED_FLAGS.items():
        assert values[flag] == default


@pytest.mark.parametrize("flag", ["DRY_RUN_MODE", "RISK_CHECKS_ENABLED"])
def test_feature_flags_default_false(monkeypatch, flag) -> None:
    values = _parse_env_example()
    monkeypatch.setenv(flag, values[flag])
    if flag == "DRY_RUN_MODE":
        assert FeatureFlags.dry_run_mode() is False
    else:
        assert FeatureFlags.risk_checks_enabled() is False


def test_max_total_notional_comes_from_env(monkeypatch) -> None:
    values = _parse_env_example()
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", values["MAX_TOTAL_NOTIONAL_USDT"])
    reset_risk_governor_for_tests()
    governor = get_risk_governor()
    assert governor.caps.max_total_notional_usdt == pytest.approx(75000.0)


def test_watchdog_flags_parse(monkeypatch) -> None:
    values = _parse_env_example()
    for flag in ("WATCHDOG_ENABLED", "WATCHDOG_AUTO_HOLD", "NOTIFY_WATCHDOG"):
        monkeypatch.setenv(flag, values[flag])
        assert exchange_watchdog_runner._env_flag(flag) is False
    monkeypatch.setenv("WATCHDOG_INTERVAL_SEC", values["WATCHDOG_INTERVAL_SEC"])
    assert exchange_watchdog_runner._env_interval() == pytest.approx(7.0)


def test_pnl_tracker_respects_env(monkeypatch) -> None:
    values = _parse_env_example()
    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", values["EXCLUDE_DRY_RUN_FROM_PNL"])
    tracker = StrategyPnlTracker()
    assert tracker.exclude_simulated_entries() is True
