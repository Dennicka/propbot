import importlib
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _write_runtime_state(path):
    payload = {
        "control": {
            "mode": "RUN",
            "safe_mode": False,
            "two_man_rule": True,
            "auto_loop": True,
            "loop_pair": "ETHUSDT",
            "loop_venues": ["binance-um", "okx-perp"],
        },
        "safety": {
            "hold_active": False,
            "hold_reason": "manual_release",
            "hold_source": "ops",
            "last_released_ts": _iso(1_700_000_000),
            "limits": {"max_orders_per_min": 12, "max_cancels_per_min": 18},
            "counters": {"orders_placed_last_min": 7, "cancels_last_min": 2},
            "resume_request": {
                "reason": "resume after hold",
                "requested_at": _iso(1_700_000_100),
                "requested_by": "alice",
            },
        },
        "auto_hedge": {
            "enabled": True,
            "consecutive_failures": 3,
            "last_execution_result": "error",
            "last_execution_ts": _iso(1_700_000_200),
            "last_success_ts": _iso(1_700_000_050),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _write_positions_store(path):
    payload = [
        {
            "id": "open-1",
            "timestamp": _iso(1_700_000_300),
            "status": "open",
            "symbol": "BTCUSDT",
            "long_venue": "binance-um",
            "short_venue": "okx-perp",
            "notional_usdt": 1000.0,
            "entry_spread_bps": 12.0,
            "leverage": 3.0,
            "entry_long_price": 25000.0,
            "entry_short_price": 25010.0,
            "pnl_usdt": 0.0,
            "legs": [
                {
                    "venue": "binance-um",
                    "symbol": "BTCUSDT",
                    "side": "long",
                    "notional_usdt": 1000.0,
                    "entry_price": 25000.0,
                    "timestamp": _iso(1_700_000_300),
                },
                {
                    "venue": "okx-perp",
                    "symbol": "BTCUSDT",
                    "side": "short",
                    "notional_usdt": 1000.0,
                    "entry_price": 25010.0,
                    "timestamp": _iso(1_700_000_300),
                },
            ],
        },
        {
            "id": "closed-1",
            "timestamp": _iso(1_700_000_310),
            "status": "closed",
            "symbol": "ETHUSDT",
            "long_venue": "binance-um",
            "short_venue": "okx-perp",
            "notional_usdt": 750.0,
            "entry_spread_bps": 8.0,
            "leverage": 2.0,
            "entry_long_price": 1800.0,
            "entry_short_price": 1803.0,
            "pnl_usdt": 15.0,
            "legs": [
                {
                    "venue": "binance-um",
                    "symbol": "ETHUSDT",
                    "side": "long",
                    "notional_usdt": 750.0,
                    "entry_price": 1800.0,
                    "timestamp": _iso(1_700_000_310),
                },
                {
                    "venue": "okx-perp",
                    "symbol": "ETHUSDT",
                    "side": "short",
                    "notional_usdt": 750.0,
                    "entry_price": 1803.0,
                    "timestamp": _iso(1_700_000_310),
                },
            ],
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def test_restart_restores_status_and_positions(monkeypatch, tmp_path):
    runtime_path = tmp_path / "runtime_state.json"
    positions_path = tmp_path / "hedge_positions.json"
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(runtime_path))
    monkeypatch.setenv("POSITIONS_STORE_PATH", str(positions_path))
    _write_runtime_state(runtime_path)
    _write_positions_store(positions_path)

    runtime_mod = importlib.reload(importlib.import_module("app.services.runtime"))
    status_mod = importlib.reload(importlib.import_module("app.services.status"))

    overview = status_mod.get_status_overview()
    assert overview["safe_mode"] is True
    assert overview["hold_active"] is True
    assert overview["hold_reason"] == "restart_safe_mode"
    assert overview["two_man_resume_required"] is True
    assert overview["auto_hedge"]["consecutive_failures"] == 3
    assert overview["runaway_guard"]["limits"]["max_orders_per_min"] == 12
    assert overview["runaway_guard"]["counters"]["orders_placed_last_min"] == 7

    components = status_mod.get_status_components()
    assert components["safe_mode"] is True
    assert components["hold_active"] is True
    assert components["runaway_guard"]["counters"]["cancels_last_min"] == 2

    slo = status_mod.get_status_slo()
    assert slo["safe_mode"] is True
    assert slo["runaway_guard"]["limits"]["max_cancels_per_min"] == 18

    main_mod = importlib.reload(importlib.import_module("app.main"))
    client = TestClient(main_mod.app)

    overview_response = client.get("/api/ui/status/overview").json()
    assert overview_response["safe_mode"] is True
    assert overview_response["hold_active"] is True
    assert overview_response["auto_hedge"]["consecutive_failures"] == 3

    positions_response = client.get("/api/ui/positions").json()
    assert {entry["id"] for entry in positions_response["positions"]} == {"open-1", "closed-1"}
    exposure = positions_response["exposure"]
    assert exposure["binance-um"]["long_notional"] == pytest.approx(1000.0)
    assert exposure["okx-perp"]["short_notional"] == pytest.approx(1000.0)
    assert "ftx" not in exposure
    closed_entry = next(item for item in positions_response["positions"] if item["id"] == "closed-1")
    assert closed_entry["unrealized_pnl_usdt"] == pytest.approx(0.0)
    assert positions_response["totals"]["unrealized_pnl_usdt"] == pytest.approx(0.0, abs=2e-3)

    runtime_mod.reset_for_tests()
