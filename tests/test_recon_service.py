from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.recon.core import ReconSnapshot
from app.recon.service import ReconDiff, collect_recon_snapshot


def _make_snapshot(**overrides: object) -> ReconSnapshot:
    defaults = {
        "venue": "binance-um",
        "asset": "USDT",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "exch_position": Decimal("1.5"),
        "local_position": Decimal("1.0"),
        "exch_balance": None,
        "local_balance": None,
        "diff_abs": Decimal("500"),
        "status": "WARN",
        "reason": "position_mismatch",
        "ts": 123.0,
    }
    defaults.update(overrides)
    return ReconSnapshot(**defaults)


def test_collect_recon_snapshot_returns_position_and_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    position_snapshot = _make_snapshot()
    balance_snapshot = _make_snapshot(
        symbol=None,
        side=None,
        exch_position=None,
        local_position=None,
        exch_balance=Decimal("1000"),
        local_balance=Decimal("1200"),
        diff_abs=Decimal("200"),
        reason="balance_mismatch",
    )

    monkeypatch.setattr(
        "app.recon.service.reconcile_once",
        lambda ctx=None: [position_snapshot, balance_snapshot],
    )

    diffs = collect_recon_snapshot(SimpleNamespace())

    assert any(isinstance(diff, ReconDiff) and diff.kind == "position" for diff in diffs)
    assert any(diff.kind == "balance" for diff in diffs)

    position = next(diff for diff in diffs if diff.kind == "position")
    assert position.diff_abs == pytest.approx(500.0)
    assert position.diff_rel is not None

    balance = next(diff for diff in diffs if diff.kind == "balance")
    assert balance.diff_abs == pytest.approx(200.0)
    assert balance.diff_rel == pytest.approx(200.0 / 1200.0)


def test_collect_recon_snapshot_filters_ok_status(monkeypatch: pytest.MonkeyPatch) -> None:
    warn_snapshot = _make_snapshot()
    ok_snapshot = _make_snapshot(status="OK", diff_abs=Decimal("0"), reason="position_ok")

    monkeypatch.setattr(
        "app.recon.service.reconcile_once",
        lambda ctx=None: [warn_snapshot, ok_snapshot],
    )

    diffs = collect_recon_snapshot(SimpleNamespace())

    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.kind == "position"
    assert diff.diff_abs == pytest.approx(500.0)
