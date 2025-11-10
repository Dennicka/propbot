from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.recon import core


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = core._ReconConfig(  # type: ignore[attr-defined]
        epsilon_position=Decimal("0.0001"),
        epsilon_balance=Decimal("0.5"),
        epsilon_notional=Decimal("5"),
        auto_hold_on_critical=True,
    )
    monkeypatch.setattr(core, "_CONFIG_OVERRIDE", config)


def test_compare_positions_detects_mismatch() -> None:
    remote = {
        ("binance-um", "BTCUSDT"): {"qty": Decimal("1.5")},
    }
    local = {}

    issues = core.compare_positions(local, remote)

    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "POSITION_MISMATCH"
    assert issue.severity == "CRITICAL"
    assert issue.venue == "binance-um"
    assert issue.symbol == "BTCUSDT"


def test_compare_balances_respects_epsilon(monkeypatch: pytest.MonkeyPatch) -> None:
    config = core._ReconConfig(  # type: ignore[attr-defined]
        epsilon_position=Decimal("0.0001"),
        epsilon_balance=Decimal("2.0"),
        epsilon_notional=Decimal("5.0"),
        auto_hold_on_critical=True,
    )
    monkeypatch.setattr(core, "_CONFIG_OVERRIDE", config)

    local = [
        {"venue": "okx-perp", "asset": "USDT", "total": Decimal("100.0")},
    ]
    remote_close = [
        {"venue": "okx-perp", "asset": "USDT", "total": Decimal("101.0")},
    ]
    assert core.compare_balances(local, remote_close) == []

    remote_far = [
        {"venue": "okx-perp", "asset": "USDT", "total": Decimal("110.0")},
    ]
    issues = core.compare_balances(local, remote_far)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "BALANCE_MISMATCH"
    assert issue.severity == "CRITICAL"
    assert issue.details.startswith("local_total=")


def test_compare_orders_flags_unknown_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    config = core._ReconConfig(  # type: ignore[attr-defined]
        epsilon_position=Decimal("0.0001"),
        epsilon_balance=Decimal("0.5"),
        epsilon_notional=Decimal("1.0"),
        auto_hold_on_critical=True,
    )
    monkeypatch.setattr(core, "_CONFIG_OVERRIDE", config)

    local = []
    remote = [
        {
            "venue": "binance-um",
            "symbol": "ETHUSDT",
            "id": "abc-1",
            "qty": Decimal("0.5"),
            "price": Decimal("10.0"),
        }
    ]

    issues = core.compare_open_orders(local, remote)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "ORDER_DESYNC"
    assert issue.severity == "CRITICAL"
    assert issue.venue == "binance-um"
    assert issue.symbol == "ETHUSDT"
