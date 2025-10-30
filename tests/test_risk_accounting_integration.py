from types import SimpleNamespace

import pytest

from app.risk import accounting as risk_accounting
from app.risk import core as risk_core

STRATEGY = "cross_exchange_arb"


@pytest.fixture(autouse=True)
def reset_accounting(monkeypatch):
    monkeypatch.delenv("MAX_TOTAL_NOTIONAL_USDT", raising=False)
    monkeypatch.delenv("MAX_OPEN_POSITIONS", raising=False)
    monkeypatch.delenv("RISK_CHECKS_ENABLED", raising=False)
    monkeypatch.delenv("RISK_ENFORCE_CAPS", raising=False)
    monkeypatch.delenv("RISK_ENFORCE_BUDGETS", raising=False)
    monkeypatch.delenv("DRY_RUN_MODE", raising=False)
    monkeypatch.setattr(
        risk_accounting,
        "get_state",
        lambda: SimpleNamespace(control=SimpleNamespace(dry_run=False)),
    )
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()
    yield
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()


def _configure_caps(monkeypatch, *, notional: float, positions: int) -> None:
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", str(notional))
    monkeypatch.setenv("MAX_OPEN_POSITIONS", str(positions))
    risk_core.reset_risk_governor_for_tests()


def test_flags_disabled_allow_large_intents(monkeypatch):
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "100.0")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("RISK_ENFORCE_CAPS", "1")
    risk_core.reset_risk_governor_for_tests()

    snapshot, ok = risk_accounting.record_intent(STRATEGY, 250.0, simulated=False)

    assert ok is True
    assert snapshot["totals"]["open_notional"] == pytest.approx(250.0)
    assert "last_denial" not in snapshot


def test_caps_enforced_when_flags_enabled(monkeypatch):
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "100.0")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("RISK_ENFORCE_CAPS", "1")
    risk_core.reset_risk_governor_for_tests()

    snapshot, ok = risk_accounting.record_intent(STRATEGY, 80.0, simulated=False)
    assert ok is True
    assert snapshot["totals"]["open_notional"] == pytest.approx(80.0)

    snapshot_after, ok_second = risk_accounting.record_intent(STRATEGY, 50.0, simulated=False)
    assert ok_second is False
    assert snapshot_after.get("last_denial", {}).get("reason") == "SKIPPED_BY_RISK"
    assert snapshot_after.get("last_denial", {}).get("details", {}).get("breach") == "max_total_notional_usdt"


def test_budget_blocks_only_when_enforced(monkeypatch):
    _configure_caps(monkeypatch, notional=1_000.0, positions=5)
    risk_accounting.set_strategy_budget_cap(STRATEGY, 50.0)

    snapshot, ok = risk_accounting.record_intent(STRATEGY, 10.0, simulated=False)
    assert ok is True
    assert snapshot["totals"]["open_positions"] == 1

    risk_accounting.record_fill(STRATEGY, 10.0, -20.0, simulated=False)
    risk_accounting.record_fill(STRATEGY, 0.0, -30.0, simulated=False)

    snapshot_before_flag, ok_without_flag = risk_accounting.record_intent(STRATEGY, 5.0, simulated=False)
    assert ok_without_flag is True
    assert "last_denial" not in snapshot_before_flag

    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("RISK_ENFORCE_BUDGETS", "1")
    risk_core.reset_risk_governor_for_tests()

    snapshot_after, ok_again = risk_accounting.record_intent(STRATEGY, 5.0, simulated=False)
    assert ok_again is False
    strategy_row = snapshot_after["per_strategy"][STRATEGY]
    assert "budget_exhausted" in strategy_row["breaches"]
    assert snapshot_after.get("last_denial", {}).get("details", {}).get("breach") == "budget_exhausted"


def test_dry_run_records_simulated_only(monkeypatch):
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "50.0")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("RISK_ENFORCE_CAPS", "1")
    risk_core.reset_risk_governor_for_tests()

    snapshot, ok = risk_accounting.record_intent(STRATEGY, 200.0, simulated=True)
    assert ok is True
    totals = snapshot["totals"]
    assert totals["open_notional"] == 0.0
    assert totals["simulated"]["open_notional"] == pytest.approx(200.0)

    snapshot_after = risk_accounting.record_fill(STRATEGY, 200.0, 7.5, simulated=True)
    totals_after = snapshot_after["totals"]
    assert totals_after["open_notional"] == 0.0
    assert totals_after["simulated"]["open_notional"] == 0.0
    assert totals_after["simulated"]["realized_pnl_today"] == pytest.approx(7.5)
