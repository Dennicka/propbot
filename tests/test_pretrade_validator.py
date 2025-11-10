from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config.schema import (
    GuardrailsConfig,
    MaintenanceConfig,
    MaintenanceScheduleWindow,
    PretradeConfig,
)
from app.rules.pretrade import PretradeValidator
from app.services import runtime


@pytest.fixture(autouse=True)
def _reset_runtime():
    runtime.reset_for_tests()
    yield


def _validator(clock: datetime) -> PretradeValidator:
    return PretradeValidator(
        local_specs={
            "BTCUSDT": {"tick": 0.5, "lot": 0.2, "min_notional": 50.0},
            "ETHUSDT": {"tick": 0.25, "lot": 0.1, "min_notional": 10.0},
        },
        clock=lambda: clock,
    )


def test_rejects_wrong_tick_and_lot_and_min_notional():
    state = runtime.get_state()
    state.control.environment = "paper"
    state.config.data = state.config.data.model_copy(
        update={
            "pretrade": PretradeConfig(allow_autofix=False, default_tz="UTC"),
            "maintenance": MaintenanceConfig(windows=[]),
            "guardrails": GuardrailsConfig(testnet_block_highrisk=False, blocklist=[]),
        }
    )
    validator = _validator(datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc))

    ok, reason, fixed = validator.validate(
        {"venue": "binance-um", "symbol": "BTCUSDT", "qty": 0.15, "price": 101.3}
    )

    assert not ok
    assert reason == "price_tick"
    assert fixed is None


def test_allows_autofix_when_enabled():
    state = runtime.get_state()
    state.control.environment = "paper"
    state.config.data = state.config.data.model_copy(
        update={
            "pretrade": PretradeConfig(allow_autofix=True, default_tz="UTC"),
            "maintenance": MaintenanceConfig(windows=[]),
            "guardrails": GuardrailsConfig(testnet_block_highrisk=False, blocklist=[]),
        }
    )
    validator = PretradeValidator(
        local_specs={
            "BTCUSDT": {"tick": 0.5, "lot": 0.1, "min_notional": 5.0},
        },
        clock=lambda: datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
    )

    ok, reason, fixed = validator.validate(
        {"venue": "binance-um", "symbol": "BTCUSDT", "qty": 2.26, "price": 101.3}
    )

    assert ok
    assert reason is None
    assert fixed == {"qty": pytest.approx(2.2), "price": pytest.approx(101.0)}


def test_blocks_outside_trade_hours_with_tz():
    state = runtime.get_state()
    state.control.environment = "paper"
    state.config.data = state.config.data.model_copy(
        update={
            "pretrade": PretradeConfig(allow_autofix=True, default_tz="UTC"),
            "maintenance": MaintenanceConfig(windows=[]),
            "guardrails": GuardrailsConfig(testnet_block_highrisk=False, blocklist=[]),
        }
    )
    validator = PretradeValidator(
        local_specs={
            "ETHUSDT": {
                "tick": 0.25,
                "lot": 0.1,
                "min_notional": 10.0,
                "trade_hours": [
                    {"from": "09:00", "to": "17:00", "tz": "Europe/Stockholm"},
                ],
            }
        },
        clock=lambda: datetime(2024, 1, 1, 5, 0, tzinfo=timezone.utc),
    )

    ok, reason, _ = validator.validate(
        {"venue": "okx-perp", "symbol": "ETHUSDT", "qty": 1.0, "price": 1500.0}
    )

    assert not ok
    assert reason == "outside_trade_hours"


def test_blocks_in_maintenance_window():
    state = runtime.get_state()
    state.control.environment = "paper"
    state.config.data = state.config.data.model_copy(
        update={
            "pretrade": PretradeConfig(allow_autofix=True, default_tz="UTC"),
            "maintenance": MaintenanceConfig(
                windows=[
                    MaintenanceScheduleWindow(
                        start="01:30",
                        end="02:30",
                        tz="UTC",
                        reason="overnight-maintenance",
                    )
                ]
            ),
            "guardrails": GuardrailsConfig(testnet_block_highrisk=False, blocklist=[]),
        }
    )
    validator = PretradeValidator(
        local_specs={"BTCUSDT": {"tick": 0.5, "lot": 0.1, "min_notional": 5.0}},
        clock=lambda: datetime(2024, 1, 1, 2, 0, tzinfo=timezone.utc),
    )

    ok, reason, _ = validator.validate(
        {"venue": "binance-um", "symbol": "BTCUSDT", "qty": 1.0, "price": 100.0}
    )

    assert not ok
    assert reason == "overnight-maintenance"


def test_guardrails_live_requires_two_man_resume():
    state = runtime.get_state()
    state.control.environment = "live"
    state.control.two_man_rule = True
    state.safety.resume_request = None
    state.config.data = state.config.data.model_copy(
        update={
            "pretrade": PretradeConfig(allow_autofix=True, default_tz="UTC"),
            "maintenance": MaintenanceConfig(windows=[]),
            "guardrails": GuardrailsConfig(testnet_block_highrisk=False, blocklist=[]),
        }
    )
    validator = PretradeValidator(
        local_specs={"BTCUSDT": {"tick": 0.5, "lot": 0.1, "min_notional": 5.0}},
        clock=lambda: datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
    )

    ok, reason, _ = validator.validate(
        {"venue": "binance-um", "symbol": "BTCUSDT", "qty": 1.0, "price": 100.0}
    )

    assert not ok
    assert reason == "two_man_resume_required"


def test_guardrails_testnet_blocks_highrisk_symbols():
    state = runtime.get_state()
    state.control.environment = "testnet"
    state.config.data = state.config.data.model_copy(
        update={
            "pretrade": PretradeConfig(allow_autofix=True, default_tz="UTC"),
            "maintenance": MaintenanceConfig(windows=[]),
            "guardrails": GuardrailsConfig(
                testnet_block_highrisk=True,
                blocklist=["BTCUSDT"],
            ),
        }
    )
    validator = PretradeValidator(
        local_specs={"BTCUSDT": {"tick": 0.5, "lot": 0.1, "min_notional": 5.0}},
        clock=lambda: datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
    )

    ok, reason, _ = validator.validate(
        {"venue": "binance-um", "symbol": "BTCUSDT", "qty": 1.0, "price": 100.0}
    )

    assert not ok
    assert reason == "symbol_blocked_testnet"
