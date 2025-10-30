from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.budget import strategy_budget as strategy_budget_module
from app.risk import accounting as risk_accounting
from app.risk import core as risk_core
from app.risk.telemetry import get_risk_skip_counts, reset_risk_skip_metrics_for_tests

STRATEGY = "cross_exchange_arb"


@pytest.fixture(autouse=True)
def reset_accounting(monkeypatch):
    monkeypatch.delenv("MAX_TOTAL_NOTIONAL_USDT", raising=False)
    monkeypatch.delenv("MAX_OPEN_POSITIONS", raising=False)
    monkeypatch.delenv("RISK_CHECKS_ENABLED", raising=False)
    monkeypatch.delenv("RISK_ENFORCE_CAPS", raising=False)
    monkeypatch.delenv("RISK_ENFORCE_BUDGETS", raising=False)
    monkeypatch.delenv("DRY_RUN_MODE", raising=False)
    monkeypatch.delenv("DAILY_LOSS_CAP_USDT", raising=False)
    monkeypatch.delenv("ENFORCE_DAILY_LOSS_CAP", raising=False)
    monkeypatch.setattr(
        risk_accounting,
        "get_state",
        lambda: SimpleNamespace(
            control=SimpleNamespace(dry_run=False, dry_run_mode=False)
        ),
    )
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()
    reset_risk_skip_metrics_for_tests()
    yield
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()
    reset_risk_skip_metrics_for_tests()


def _configure_caps(monkeypatch, *, notional: float, positions: int) -> None:
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", str(notional))
    monkeypatch.setenv("MAX_OPEN_POSITIONS", str(positions))
    risk_core.reset_risk_governor_for_tests()


def test_flags_disabled_allow_large_intents(monkeypatch):
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "100.0")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("RISK_ENFORCE_CAPS", "1")
    risk_core.reset_risk_governor_for_tests()

    snapshot, result = risk_accounting.record_intent(STRATEGY, 250.0, simulated=False)

    assert result["ok"] is True
    assert snapshot["totals"]["open_notional"] == pytest.approx(250.0)
    assert "last_denial" not in snapshot


def test_caps_enforced_when_flags_enabled(monkeypatch):
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "100.0")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("RISK_ENFORCE_CAPS", "1")
    risk_core.reset_risk_governor_for_tests()

    snapshot, result = risk_accounting.record_intent(STRATEGY, 80.0, simulated=False)
    assert result["ok"] is True
    assert snapshot["totals"]["open_notional"] == pytest.approx(80.0)

    snapshot_after, result_second = risk_accounting.record_intent(STRATEGY, 50.0, simulated=False)
    assert result_second["ok"] is False
    assert result_second["reason"] == "caps_exceeded"
    assert snapshot_after.get("last_denial", {}).get("reason") == "caps_exceeded"
    assert snapshot_after.get("last_denial", {}).get("details", {}).get("breach") == "max_total_notional_usdt"


def test_budget_blocks_only_when_enforced(monkeypatch):
    _configure_caps(monkeypatch, notional=1_000.0, positions=5)
    risk_accounting.set_strategy_budget_cap(STRATEGY, 50.0)

    snapshot, result = risk_accounting.record_intent(STRATEGY, 10.0, simulated=False)
    assert result["ok"] is True
    assert snapshot["totals"]["open_positions"] == 1

    risk_accounting.record_fill(STRATEGY, 10.0, -20.0, simulated=False)
    risk_accounting.record_fill(STRATEGY, 0.0, -30.0, simulated=False)

    snapshot_before_flag, result_without_flag = risk_accounting.record_intent(
        STRATEGY, 5.0, simulated=False
    )
    assert result_without_flag["ok"] is True
    assert "last_denial" not in snapshot_before_flag
    budget_before = snapshot_before_flag["per_strategy"][STRATEGY]["budget"]
    assert budget_before["used_today_usdt"] == pytest.approx(50.0)
    assert budget_before["remaining_usdt"] == pytest.approx(0.0)
    assert snapshot_before_flag["per_strategy"][STRATEGY]["blocked_by_budget"] is True

    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("RISK_ENFORCE_BUDGETS", "1")
    risk_core.reset_risk_governor_for_tests()

    snapshot_after, result_again = risk_accounting.record_intent(
        STRATEGY, 5.0, simulated=False
    )
    assert result_again["ok"] is False
    assert result_again["reason"] == "budget_exceeded"
    strategy_row = snapshot_after["per_strategy"][STRATEGY]
    assert "budget_exhausted" in strategy_row["breaches"]
    assert strategy_row["blocked_by_budget"] is True
    last_denial = snapshot_after.get("last_denial", {})
    assert last_denial.get("state") == "SKIPPED_BY_RISK"
    assert last_denial.get("reason") == "budget_exceeded"
    counts = get_risk_skip_counts()
    assert counts.get(STRATEGY, {}).get("budget_exceeded") == 1


def test_dry_run_records_simulated_only(monkeypatch):
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "50.0")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("RISK_ENFORCE_CAPS", "1")
    risk_core.reset_risk_governor_for_tests()

    snapshot, result = risk_accounting.record_intent(STRATEGY, 200.0, simulated=True)
    assert result["ok"] is True
    totals = snapshot["totals"]
    assert totals["open_notional"] == 0.0
    assert totals["simulated"]["open_notional"] == pytest.approx(200.0)

    snapshot_after = risk_accounting.record_fill(STRATEGY, 200.0, 7.5, simulated=True)
    totals_after = snapshot_after["totals"]
    assert totals_after["open_notional"] == 0.0
    assert totals_after["simulated"]["open_notional"] == 0.0
    assert totals_after["simulated"]["realized_pnl_today"] == pytest.approx(7.5)


def test_budget_requires_both_feature_flags(monkeypatch):
    _configure_caps(monkeypatch, notional=1_000.0, positions=5)
    risk_accounting.set_strategy_budget_cap(STRATEGY, 25.0)
    risk_accounting.record_fill(STRATEGY, 0.0, -30.0, simulated=False)

    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    risk_core.reset_risk_governor_for_tests()
    snapshot_checks_only, result_checks_only = risk_accounting.record_intent(
        STRATEGY, 1.0, simulated=False
    )
    assert result_checks_only["ok"] is True
    assert snapshot_checks_only["per_strategy"][STRATEGY]["blocked_by_budget"] is True

    monkeypatch.setenv("RISK_ENFORCE_BUDGETS", "1")
    monkeypatch.delenv("RISK_CHECKS_ENABLED", raising=False)
    risk_core.reset_risk_governor_for_tests()
    snapshot_budgets_only, result_budgets_only = risk_accounting.record_intent(
        STRATEGY, 1.0, simulated=False
    )
    assert result_budgets_only["ok"] is True
    assert snapshot_budgets_only["per_strategy"][STRATEGY]["blocked_by_budget"] is True


def test_budget_auto_resets_at_new_utc_day(monkeypatch):
    start = datetime(2024, 3, 9, 10, 0, tzinfo=timezone.utc)
    next_day = start + timedelta(days=1)

    monkeypatch.setattr(strategy_budget_module, "_utc_now", lambda: start)
    risk_accounting.set_strategy_budget_cap(STRATEGY, 100.0)
    risk_accounting.record_fill(STRATEGY, 0.0, -60.0, simulated=False)
    snapshot_before = risk_accounting.get_risk_snapshot()
    assert snapshot_before["per_strategy"][STRATEGY]["budget"]["used_today_usdt"] == pytest.approx(60.0)

    monkeypatch.setattr(strategy_budget_module, "_utc_now", lambda: next_day)
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("RISK_ENFORCE_BUDGETS", "1")
    risk_core.reset_risk_governor_for_tests()
    snapshot_after, result = risk_accounting.record_intent(STRATEGY, 1.0, simulated=False)
    assert result["ok"] is True
    budget_after = snapshot_after["per_strategy"][STRATEGY]["budget"]
    assert budget_after["used_today_usdt"] == pytest.approx(0.0)
    assert budget_after["last_reset_ts_utc"].startswith(next_day.date().isoformat())


def test_budget_not_blocked_in_runtime_dry_run_mode(monkeypatch):
    _configure_caps(monkeypatch, notional=1_000.0, positions=5)
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("RISK_ENFORCE_BUDGETS", "1")
    risk_core.reset_risk_governor_for_tests()
    risk_accounting.set_strategy_budget_cap(STRATEGY, 10.0)
    risk_accounting.record_fill(STRATEGY, 0.0, -15.0, simulated=False)

    monkeypatch.setattr(
        risk_accounting,
        "get_state",
        lambda: SimpleNamespace(
            control=SimpleNamespace(dry_run=False, dry_run_mode=True)
        ),
    )

    snapshot, result = risk_accounting.record_intent(STRATEGY, 1.0, simulated=False)
    assert result["ok"] is True
    strategy_row = snapshot["per_strategy"][STRATEGY]
    assert strategy_row["blocked_by_budget"] is True
    assert snapshot.get("last_denial") is None


def test_daily_loss_cap_blocks_when_breached(monkeypatch):
    _configure_caps(monkeypatch, notional=10_000.0, positions=50)
    monkeypatch.setenv("DAILY_LOSS_CAP_USDT", "100")
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("ENFORCE_DAILY_LOSS_CAP", "1")
    risk_core.reset_risk_governor_for_tests()

    risk_accounting.record_fill(STRATEGY, 0.0, -120.0, simulated=False)

    snapshot, result = risk_accounting.record_intent(STRATEGY, 10.0, simulated=False)

    assert result["ok"] is False
    assert result["reason"] == "DAILY_LOSS_CAP"
    assert result["state"] == "SKIPPED_BY_RISK"
    last_denial = snapshot.get("last_denial", {})
    assert last_denial.get("reason") == "DAILY_LOSS_CAP"
    bot_cap = snapshot.get("bot_loss_cap", {})
    assert bot_cap.get("breached") is True
    daily_cap = snapshot.get("daily_loss_cap", {})
    assert daily_cap.get("losses_usdt") == pytest.approx(120.0)
    assert bot_cap.get("realized_today_usdt") == pytest.approx(-120.0)
    counts = get_risk_skip_counts()
    assert counts.get(STRATEGY, {}).get("daily_loss_cap") == 1


@pytest.mark.parametrize("scenario", ["checks_disabled", "enforcement_disabled", "dry_run"])
def test_daily_loss_cap_not_enforced_when_disabled(monkeypatch, scenario):
    _configure_caps(monkeypatch, notional=10_000.0, positions=50)
    monkeypatch.setenv("DAILY_LOSS_CAP_USDT", "100")
    risk_accounting.record_fill(STRATEGY, 0.0, -80.0, simulated=False)

    if scenario != "checks_disabled":
        monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    if scenario != "enforcement_disabled":
        monkeypatch.setenv("ENFORCE_DAILY_LOSS_CAP", "1")
    if scenario == "dry_run":
        monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
        monkeypatch.setenv("ENFORCE_DAILY_LOSS_CAP", "1")
        monkeypatch.setattr(
            risk_accounting,
            "get_state",
            lambda: SimpleNamespace(
                control=SimpleNamespace(dry_run=True, dry_run_mode=False)
            ),
        )

    risk_core.reset_risk_governor_for_tests()
    snapshot, result = risk_accounting.record_intent(STRATEGY, 10.0, simulated=False)

    assert result["ok"] is True
    bot_cap = snapshot.get("bot_loss_cap", {})
    assert bot_cap.get("cap_usdt") == pytest.approx(100.0)
    assert bot_cap.get("realized_today_usdt") == pytest.approx(-80.0)
    assert bot_cap.get("breached") is False


def test_daily_loss_cap_auto_resets_at_utc_midnight(monkeypatch):
    start = datetime(2024, 3, 9, 22, 30, tzinfo=timezone.utc)
    next_day = start + timedelta(days=1)

    monkeypatch.setattr(risk_accounting, "_utc_now", lambda: start)
    monkeypatch.setattr("app.risk.daily_loss._utc_now", lambda: start)
    _configure_caps(monkeypatch, notional=10_000.0, positions=50)
    monkeypatch.setenv("DAILY_LOSS_CAP_USDT", "200")
    risk_core.reset_risk_governor_for_tests()

    risk_accounting.record_fill(STRATEGY, 0.0, -40.0, simulated=False)
    snapshot_before = risk_accounting.get_risk_snapshot()
    cap_before = snapshot_before.get("bot_loss_cap", {})
    assert cap_before.get("realized_today_usdt") == pytest.approx(-40.0)
    assert cap_before.get("remaining_usdt") == pytest.approx(160.0)

    monkeypatch.setattr(risk_accounting, "_utc_now", lambda: next_day)
    monkeypatch.setattr("app.risk.daily_loss._utc_now", lambda: next_day)
    snapshot_after = risk_accounting.get_risk_snapshot()
    cap_after = snapshot_after.get("bot_loss_cap", {})
    assert cap_after.get("realized_today_usdt") == pytest.approx(0.0)
    assert cap_after.get("remaining_usdt") == pytest.approx(200.0)
