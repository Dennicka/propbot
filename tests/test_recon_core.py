from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.recon.core import reconcile_once


@dataclass
class _FakeSafety:
    risk_snapshot: dict[str, Any]
    hold_active: bool = False
    hold_reason: str | None = None
    reconciliation_snapshot: dict[str, Any] = None

    def status_payload(self) -> dict[str, Any]:  # pragma: no cover - helper
        return {
            "hold_active": self.hold_active,
            "hold_reason": self.hold_reason,
            "reconciliation": self.reconciliation_snapshot or {},
        }


@pytest.fixture
def fake_state(monkeypatch: pytest.MonkeyPatch) -> _FakeSafety:
    safety = _FakeSafety(risk_snapshot={})
    state = SimpleNamespace(safety=safety, config=SimpleNamespace(data=None))
    monkeypatch.setattr("app.recon.core.runtime.get_state", lambda: state)
    return safety


def test_reconcile_classifies_ok_warn_critical(monkeypatch: pytest.MonkeyPatch, fake_state: _FakeSafety) -> None:
    fake_state.risk_snapshot = {}

    exchange_positions = {
        ("binance-um", "BTCUSDT"): Decimal("1"),
        ("binance-um", "ETHUSDT"): Decimal("1.5"),
        ("binance-um", "SOLUSDT"): Decimal("0"),
    }
    ledger_positions = {
        ("binance-um", "BTCUSDT"): {"qty": Decimal("1"), "avg_price": Decimal("20000")},
        ("binance-um", "ETHUSDT"): {"qty": Decimal("1"), "avg_price": Decimal("10")},
        ("binance-um", "SOLUSDT"): {"qty": Decimal("1"), "avg_price": Decimal("100")},
    }

    monkeypatch.setattr(
        "app.recon.core.Reconciler.fetch_exchange_positions",
        lambda self: exchange_positions,
    )
    monkeypatch.setattr(
        "app.recon.core.Reconciler.fetch_ledger_positions",
        lambda self: ledger_positions,
    )
    monkeypatch.setattr(
        "app.recon.core.Reconciler._fetch_mark_prices",
        lambda self, candidates: {"BTCUSDT": 20000.0, "ETHUSDT": 10.0, "SOLUSDT": 100.0},
    )

    ctx = SimpleNamespace(
        cfg=SimpleNamespace(
            recon=SimpleNamespace(
                warn_notional_usd=Decimal("5"),
                critical_notional_usd=Decimal("25"),
            )
        ),
        local_balances=lambda: [
            {"venue": "binance-um", "asset": "USDT", "total": "1000"},
        ],
        remote_balances=lambda: [
            {"venue": "binance-um", "asset": "USDT", "total": "1005"},
        ],
    )

    snapshots = reconcile_once(ctx)

    status_map = {(snap.venue, snap.symbol or snap.asset): snap.status for snap in snapshots}
    assert status_map[("binance-um", "BTCUSDT")] == "OK"
    assert status_map[("binance-um", "ETHUSDT")] == "WARN"
    assert status_map[("binance-um", "SOLUSDT")] == "CRITICAL"
    assert status_map[("binance-um", "USDT")] in {"OK", "WARN", "CRITICAL"}
    assert all(isinstance(snap.diff_abs, Decimal) for snap in snapshots)


def test_reconcile_uses_decimal_and_config(monkeypatch: pytest.MonkeyPatch, fake_state: _FakeSafety) -> None:
    exchange_positions = {("okx-perp", "ADAUSDT"): Decimal("10")}
    ledger_positions = {("okx-perp", "ADAUSDT"): {"qty": Decimal("8"), "avg_price": Decimal("1")}}
    monkeypatch.setattr(
        "app.recon.core.Reconciler.fetch_exchange_positions",
        lambda self: exchange_positions,
    )
    monkeypatch.setattr(
        "app.recon.core.Reconciler.fetch_ledger_positions",
        lambda self: ledger_positions,
    )
    monkeypatch.setattr(
        "app.recon.core.Reconciler._fetch_mark_prices",
        lambda self, candidates: {"ADAUSDT": 1.0},
    )

    ctx = SimpleNamespace(
        cfg=SimpleNamespace(
            recon=SimpleNamespace(
                warn_notional_usd=Decimal("1"),
                critical_notional_usd=Decimal("5"),
            )
        ),
        local_balances=lambda: [],
        remote_balances=lambda: [],
    )

    snapshots = reconcile_once(ctx)
    ada_snapshot = next(s for s in snapshots if s.symbol == "ADAUSDT")
    assert isinstance(ada_snapshot.diff_abs, Decimal)
    assert ada_snapshot.diff_abs == Decimal("2")
    assert ada_snapshot.status == "WARN"
