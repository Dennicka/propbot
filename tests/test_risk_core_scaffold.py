import importlib

import pytest

from app.budget.strategy_budget import BudgetValidationError, StrategyBudgetManager
from app.risk.core import RiskCaps, RiskGovernor, RiskValidationError


class TestRiskCaps:
    def test_requires_positive_values(self) -> None:
        caps = RiskCaps(
            max_open_positions=3,
            max_total_notional_usdt=100_000,
            max_notional_per_exchange={"binance-um": 50_000},
        )
        assert caps.max_open_positions == 3
        assert caps.max_total_notional_usdt == 100_000.0
        assert caps.max_notional_per_exchange["binance-um"] == 50_000.0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_open_positions": 0, "max_total_notional_usdt": 1},
            {"max_open_positions": -1, "max_total_notional_usdt": 1},
            {"max_open_positions": 1, "max_total_notional_usdt": 0},
            {"max_open_positions": 1, "max_total_notional_usdt": -1},
            {
                "max_open_positions": 1,
                "max_total_notional_usdt": 1,
                "max_notional_per_exchange": {"binance": -1},
            },
        ],
    )
    def test_rejects_non_positive_values(self, kwargs) -> None:
        with pytest.raises(RiskValidationError):
            RiskCaps(**kwargs)


class TestRiskGovernor:
    def setup_method(self) -> None:
        caps = RiskCaps(
            max_open_positions=5,
            max_total_notional_usdt=100_000,
            max_notional_per_exchange={"binance-um": 50_000},
        )
        self.governor = RiskGovernor(caps)

    def test_valid_limits_pass(self) -> None:
        self.governor.ensure_open_positions_within_limit(3)
        self.governor.ensure_total_notional_within_limit(40_000)
        self.governor.ensure_exchange_notional_within_limit("binance-um", 10_000)

    @pytest.mark.parametrize("value", [-1, None])
    def test_rejects_invalid_position_counts(self, value) -> None:
        with pytest.raises(RiskValidationError):
            self.governor.ensure_open_positions_within_limit(value)  # type: ignore[arg-type]

    def test_rejects_position_overflow(self) -> None:
        with pytest.raises(RiskValidationError):
            self.governor.ensure_open_positions_within_limit(10)

    @pytest.mark.parametrize("value", [-1.0, None])
    def test_rejects_invalid_total_notional(self, value) -> None:
        with pytest.raises(RiskValidationError):
            self.governor.ensure_total_notional_within_limit(value)  # type: ignore[arg-type]

    def test_rejects_total_notional_overflow(self) -> None:
        with pytest.raises(RiskValidationError):
            self.governor.ensure_total_notional_within_limit(200_000)

    @pytest.mark.parametrize("value", [-1.0, None])
    def test_rejects_invalid_exchange_notional(self, value) -> None:
        with pytest.raises(RiskValidationError):
            self.governor.ensure_exchange_notional_within_limit("binance-um", value)  # type: ignore[arg-type]

    def test_rejects_exchange_overflow(self) -> None:
        with pytest.raises(RiskValidationError):
            self.governor.ensure_exchange_notional_within_limit("binance-um", 60_000)

    def test_unconfigured_exchange_is_unbounded(self) -> None:
        # Should not raise when cap is not configured
        self.governor.ensure_exchange_notional_within_limit("okx-perp", 999_999)


class TestStrategyBudgetManager:
    def test_positive_caps_and_allocations(self) -> None:
        manager = StrategyBudgetManager()
        manager.set_cap("alpha", 10_000)
        manager.allocate("alpha", 5_000)
        assert manager.get_allocation("alpha") == 5_000
        assert manager.get_remaining("alpha") == 5_000
        manager.release("alpha", 2_000)
        assert manager.get_allocation("alpha") == 3_000

    def test_rejects_invalid_cap_and_allocation(self) -> None:
        manager = StrategyBudgetManager()
        with pytest.raises(BudgetValidationError):
            manager.set_cap("alpha", -1)
        manager.set_cap("alpha", 5_000)
        with pytest.raises(BudgetValidationError):
            manager.allocate("alpha", -10)
        with pytest.raises(BudgetValidationError):
            manager.allocate("alpha", 6_000)
        manager.allocate("alpha", 1_000)
        with pytest.raises(BudgetValidationError):
            manager.release("alpha", 2_000)


class TestFeatureFlags:
    def test_risk_checks_disabled_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("RISK_CHECKS_ENABLED", raising=False)
        module = importlib.import_module("app.risk.core")
        importlib.reload(module)
        assert module.FeatureFlags.risk_checks_enabled() is False
