import pytest

from app.risk import accounting as risk_accounting
from app.risk import core as risk_core


STRATEGY = "cross_exchange_arb"


@pytest.fixture(autouse=True)
def reset_accounting(monkeypatch):
    monkeypatch.delenv("MAX_TOTAL_NOTIONAL_USDT", raising=False)
    monkeypatch.delenv("MAX_OPEN_POSITIONS", raising=False)
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()
    yield
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()


def _configure_caps(monkeypatch, *, notional: float, positions: int) -> None:
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", str(notional))
    monkeypatch.setenv("MAX_OPEN_POSITIONS", str(positions))
    risk_core.reset_risk_governor_for_tests()


def test_caps_block_additional_intents(monkeypatch):
    _configure_caps(monkeypatch, notional=100.0, positions=1)

    snapshot, ok = risk_accounting.record_intent(STRATEGY, 80.0, simulated=False)
    assert ok is True
    assert snapshot["totals"]["open_notional"] == pytest.approx(80.0)

    snapshot_after, ok_second = risk_accounting.record_intent(STRATEGY, 30.0, simulated=False)
    assert ok_second is False
    assert snapshot_after["totals"]["open_notional"] == pytest.approx(80.0)


def test_budget_blocks_after_losses(monkeypatch):
    _configure_caps(monkeypatch, notional=1_000.0, positions=5)
    risk_accounting.set_strategy_budget_cap(STRATEGY, 50.0)

    snapshot, ok = risk_accounting.record_intent(STRATEGY, 10.0, simulated=False)
    assert ok is True
    assert snapshot["totals"]["open_positions"] == 1

    risk_accounting.record_fill(STRATEGY, 10.0, -20.0, simulated=False)
    risk_accounting.record_fill(STRATEGY, 0.0, -30.0, simulated=False)

    snapshot_after, ok_again = risk_accounting.record_intent(STRATEGY, 5.0, simulated=False)
    assert ok_again is False
    strategy_row = snapshot_after["per_strategy"][STRATEGY]
    assert "budget_exhausted" in strategy_row["breaches"]
    assert strategy_row["budget"]["used"] == pytest.approx(50.0)


def test_simulated_runs_only_touch_simulated_counters(monkeypatch):
    _configure_caps(monkeypatch, notional=50.0, positions=1)
    risk_accounting.set_strategy_budget_cap(STRATEGY, 5.0)

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
