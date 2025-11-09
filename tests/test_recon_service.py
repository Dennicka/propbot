from types import SimpleNamespace

import pytest

from app.recon.service import ReconDiff, collect_recon_snapshot


def _position_diff() -> dict[str, object]:
    return {
        "venue": "binance-um",
        "symbol": "BTCUSDT",
        "ledger_qty": 1.0,
        "exch_qty": 1.5,
        "delta": 0.5,
        "notional_usd": 500.0,
    }


def test_collect_recon_snapshot_returns_position_and_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.recon.service.Reconciler.diff", lambda self: [_position_diff()])
    ctx = SimpleNamespace(
        local_balances=lambda: [{"venue": "binance-um", "asset": "USDT", "qty": 1200.0}],
        remote_balances=lambda: [{"venue": "binance-um", "asset": "USDT", "total": 1000.0}],
    )

    diffs = collect_recon_snapshot(ctx)

    assert any(isinstance(diff, ReconDiff) and diff.kind == "position" for diff in diffs)
    assert any(diff.kind == "balance" for diff in diffs)

    position = next(diff for diff in diffs if diff.kind == "position")
    assert position.diff_abs == pytest.approx(500.0)
    assert position.diff_rel is not None

    balance = next(diff for diff in diffs if diff.kind == "balance")
    assert balance.diff_abs == pytest.approx(200.0)
    assert balance.diff_rel == pytest.approx(200.0 / 1200.0)


def test_collect_recon_snapshot_uses_asset_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.recon.service.Reconciler.diff", lambda self: [])
    ctx = SimpleNamespace(
        local_balances=lambda: [{"venue": "okx-perp", "asset": "BTC", "qty": 1.0}],
        remote_balances=lambda: [{"venue": "okx-perp", "asset": "BTC", "total": 1.2}],
        asset_prices={"BTC": 25_000.0},
    )

    diffs = collect_recon_snapshot(ctx)

    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.kind == "balance"
    assert diff.diff_abs == pytest.approx(0.2 * 25_000.0)
    assert diff.diff_rel == pytest.approx(0.2 / 1.2)
