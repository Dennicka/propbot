from __future__ import annotations

import json

import pytest

from app.services import runtime


def _set_runtime_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    runtime_file = tmp_path / "runtime_state.json"
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(runtime_file))
    runtime.reset_for_tests()


def test_apply_control_patch_ignores_none(monkeypatch, tmp_path):
    _set_runtime_path(monkeypatch, tmp_path)

    control = runtime.get_state().control
    original_notional = control.order_notional_usdt

    control_after, changes = runtime.apply_control_patch(
        {"order_notional_usdt": None, "max_slippage_bps": 10}
    )

    assert control_after.max_slippage_bps == 10
    assert control_after.order_notional_usdt == pytest.approx(original_notional)
    assert changes == {"max_slippage_bps": 10}


def test_apply_control_patch_rejects_invalid_range(monkeypatch, tmp_path):
    _set_runtime_path(monkeypatch, tmp_path)

    with pytest.raises(ValueError):
        runtime.apply_control_patch({"max_slippage_bps": 75})

    with pytest.raises(ValueError):
        runtime.apply_control_patch({"order_notional_usdt": 0.5})

    with pytest.raises(ValueError):
        runtime.apply_control_patch({"min_spread_bps": 150})


def test_control_patch_persists_and_loads(monkeypatch, tmp_path):
    _set_runtime_path(monkeypatch, tmp_path)

    payload = {
        "order_notional_usdt": 2500,
        "max_slippage_bps": 5,
        "min_spread_bps": 10,
        "loop_pair": "ethusdt",
        "loop_venues": ["binance-um", "okx-perp"],
    }

    runtime.apply_control_patch(payload)

    runtime_file = tmp_path / "runtime_state.json"
    assert runtime_file.exists()

    data = json.loads(runtime_file.read_text())
    stored = data["control"]
    assert stored["order_notional_usdt"] == pytest.approx(2500)
    assert stored["max_slippage_bps"] == 5
    assert stored["min_spread_bps"] == 10
    assert stored["loop_pair"] == "ETHUSDT"
    assert stored["loop_venues"] == ["binance-um", "okx-perp"]

    runtime.reset_for_tests()
    reloaded = runtime.get_state().control
    assert reloaded.order_notional_usdt == pytest.approx(2500)
    assert reloaded.max_slippage_bps == 5
    assert reloaded.min_spread_bps == pytest.approx(10)
    assert reloaded.loop_pair == "ETHUSDT"
    assert reloaded.loop_venues == ["binance-um", "okx-perp"]
