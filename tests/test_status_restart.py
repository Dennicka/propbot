import asyncio
import importlib
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def test_restart_restores_status_and_positions(monkeypatch, tmp_path):
    runtime_path = tmp_path / "runtime_state.json"
    positions_path = tmp_path / "hedge_positions.json"
    snapshot_path = tmp_path / "runtime_snapshot.json"
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(runtime_path))
    monkeypatch.setenv("POSITIONS_STORE_PATH", str(positions_path))
    monkeypatch.setenv("RUNTIME_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("APPROVE_TOKEN", "pytest-approve")
    monkeypatch.setenv("INCIDENT_RESTORE_ON_START", "true")
    monkeypatch.setenv("MAX_ORDERS_PER_MIN", "12")
    monkeypatch.setenv("MAX_CANCELS_PER_MIN", "18")

    runtime_mod = importlib.reload(importlib.import_module("app.services.runtime"))
    positions_mod = importlib.reload(importlib.import_module("positions"))
    status_mod = importlib.reload(importlib.import_module("app.services.status"))

    open_positions = [
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
            "entry_short_price": 25000.0,
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
                    "entry_price": 25000.0,
                    "timestamp": _iso(1_700_000_300),
                },
            ],
        },
        {
            "id": "open-2",
            "timestamp": _iso(1_700_000_400),
            "status": "open",
            "symbol": "ETHUSDT",
            "long_venue": "binance-um",
            "short_venue": "okx-perp",
            "notional_usdt": 750.0,
            "entry_spread_bps": 8.0,
            "leverage": 2.0,
            "entry_long_price": 1800.0,
            "entry_short_price": 1800.0,
            "pnl_usdt": 0.0,
            "legs": [
                {
                    "venue": "binance-um",
                    "symbol": "ETHUSDT",
                    "side": "long",
                    "notional_usdt": 750.0,
                    "entry_price": 1800.0,
                    "timestamp": _iso(1_700_000_400),
                },
                {
                    "venue": "okx-perp",
                    "symbol": "ETHUSDT",
                    "side": "short",
                    "notional_usdt": 750.0,
                    "entry_price": 1800.0,
                    "timestamp": _iso(1_700_000_400),
                },
            ],
        },
        {
            "id": "open-3",
            "timestamp": _iso(1_700_000_500),
            "status": "open",
            "symbol": "SOLUSDT",
            "long_venue": "binance-um",
            "short_venue": "okx-perp",
            "notional_usdt": 500.0,
            "entry_spread_bps": 6.5,
            "leverage": 2.5,
            "entry_long_price": 30.0,
            "entry_short_price": 30.0,
            "pnl_usdt": 0.0,
            "legs": [
                {
                    "venue": "binance-um",
                    "symbol": "SOLUSDT",
                    "side": "long",
                    "notional_usdt": 500.0,
                    "entry_price": 30.0,
                    "timestamp": _iso(1_700_000_500),
                },
                {
                    "venue": "okx-perp",
                    "symbol": "SOLUSDT",
                    "side": "short",
                    "notional_usdt": 500.0,
                    "entry_price": 30.0,
                    "timestamp": _iso(1_700_000_500),
                },
            ],
        },
    ]

    runtime_mod.set_positions_state(open_positions)
    runtime_mod.engage_safety_hold("ops_pause", source="ops")

    asyncio.run(runtime_mod.on_shutdown(reason="pytest"))
    assert snapshot_path.exists()

    if runtime_path.exists():
        runtime_path.unlink()
    if positions_path.exists():
        positions_path.unlink()

    runtime_mod = importlib.reload(importlib.import_module("app.services.runtime"))
    positions_mod = importlib.reload(importlib.import_module("positions"))
    status_mod = importlib.reload(importlib.import_module("app.services.status"))

    overview = status_mod.get_status_overview()
    assert overview["safe_mode"] is True
    assert overview["hold_active"] is True
    assert overview["hold_reason"] == "ops_pause"
    assert overview["two_man_resume_required"] is True
    assert overview["runaway_guard"]["limits"]["max_orders_per_min"] == 12
    assert overview["runaway_guard"]["counters"]["orders_placed_last_min"] == 0

    components = status_mod.get_status_components()
    assert components["safe_mode"] is True
    assert components["hold_active"] is True
    assert components["runaway_guard"]["counters"]["cancels_last_min"] == 0

    slo = status_mod.get_status_slo()
    assert slo["safe_mode"] is True
    assert slo["runaway_guard"]["limits"]["max_cancels_per_min"] == 18

    main_mod = importlib.reload(importlib.import_module("app.main"))
    client = TestClient(main_mod.app)

    overview_response = client.get("/api/ui/status/overview").json()
    assert overview_response["safe_mode"] is True
    assert overview_response["hold_active"] is True
    assert overview_response["hold_reason"] == "ops_pause"

    positions_response = client.get("/api/ui/positions").json()
    assert {entry["id"] for entry in positions_response["positions"]} == {
        "open-1",
        "open-2",
        "open-3",
    }
    exposure = positions_response["exposure"]
    assert exposure["binance-um"]["long_notional"] == pytest.approx(2250.0)
    assert exposure["okx-perp"]["short_notional"] == pytest.approx(2250.0)
    assert positions_response["totals"]["unrealized_pnl_usdt"] == pytest.approx(0.0, abs=2e-3)

    runtime_mod.reset_for_tests()
