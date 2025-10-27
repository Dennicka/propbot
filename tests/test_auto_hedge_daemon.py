from __future__ import annotations

from typing import Dict

from typing import Dict

import pytest

from app.auto_hedge_daemon import AutoHedgeDaemon
from app.services import runtime
from app.services.hedge_log import read_entries, reset_log
from app.services.status import get_status_overview
from positions import list_positions
from services import cross_exchange_arb
from services.opportunity_scanner import get_scanner


@pytest.mark.asyncio
async def test_auto_daemon_executes_success(monkeypatch, tmp_path) -> None:
    runtime.reset_for_tests()
    runtime.record_resume_request("auto_daemon_success", requested_by="pytest")
    runtime.approve_resume(actor="pytest")
    state = runtime.get_state()
    state.control.safe_mode = False
    state.control.mode = "RUN"
    reset_log()
    monkeypatch.setenv("AUTO_HEDGE_ENABLED", "true")
    monkeypatch.setenv("HEDGE_LOG_PATH", str(tmp_path / "hedge_log.json"))

    candidate = {
        "id": "abc",
        "symbol": "BTCUSDT",
        "long_venue": "binance-um",
        "short_venue": "okx-perp",
        "spread": 10.0,
        "spread_bps": 25.0,
        "min_spread": 5.0,
        "notional_suggestion": 1000.0,
        "leverage_suggestion": 2.0,
    }

    async def fake_scan_once():
        return {"candidate": candidate, "status": "allowed"}

    monkeypatch.setattr(get_scanner(), "scan_once", fake_scan_once)

    def fake_execute(symbol, notional, leverage, min_spread):
        return {
            "success": True,
            "cheap_exchange": "binance-um",
            "expensive_exchange": "okx-perp",
            "spread_bps": 18.2,
            "status": "executed",
            "legs": [
                {
                    "side": "long",
                    "venue": "binance-um",
                    "price": 21000.0,
                    "avg_price": 21000.0,
                    "notional_usdt": notional,
                    "status": "filled",
                },
                {
                    "side": "short",
                    "venue": "okx-perp",
                    "price": 21010.0,
                    "avg_price": 21010.0,
                    "notional_usdt": notional,
                    "status": "filled",
                },
            ],
            "long_order": {"price": 21000.0, "avg_price": 21000.0, "status": "filled"},
            "short_order": {"price": 21010.0, "avg_price": 21010.0, "status": "filled"},
        }

    monkeypatch.setattr("app.auto_hedge_daemon.execute_hedged_trade", fake_execute)

    daemon = AutoHedgeDaemon()
    await daemon.run_cycle()

    positions = list_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "BTCUSDT"

    entries = read_entries()
    assert len(entries) == 1
    assert entries[0]["result"] == "accepted"
    assert entries[0]["initiator"] == "YOUR_NAME_OR_TOKEN"

    state = runtime.get_state()
    assert state.auto_hedge.last_execution_result == "ok"
    assert state.auto_hedge.consecutive_failures == 0


@pytest.mark.asyncio
async def test_auto_daemon_skips_when_hold_active(monkeypatch, tmp_path) -> None:
    runtime.reset_for_tests()
    state = runtime.get_state()
    state.control.safe_mode = False
    state.control.mode = "RUN"
    reset_log()
    monkeypatch.setenv("AUTO_HEDGE_ENABLED", "true")
    monkeypatch.setenv("HEDGE_LOG_PATH", str(tmp_path / "hedge_log.json"))

    async def fake_scan_once():
        pytest.fail("scanner should not run when hold is active")

    monkeypatch.setattr(get_scanner(), "scan_once", fake_scan_once)
    monkeypatch.setattr(
        "app.auto_hedge_daemon.execute_hedged_trade",
        lambda *args, **kwargs: pytest.fail("execute should not run when hold is active"),
    )

    runtime.engage_safety_hold("test_hold", source="test")

    daemon = AutoHedgeDaemon()
    await daemon.run_cycle()

    entries = read_entries()
    assert entries == []
    state = runtime.get_state()
    assert state.auto_hedge.last_execution_result == "rejected: hold_active"
    assert runtime.is_hold_active()


@pytest.mark.asyncio
async def test_auto_daemon_triggers_hold_after_failures(monkeypatch, tmp_path) -> None:
    runtime.reset_for_tests()
    runtime.record_resume_request("auto_daemon_failures", requested_by="pytest")
    runtime.approve_resume(actor="pytest")
    state = runtime.get_state()
    state.control.safe_mode = False
    state.control.mode = "RUN"
    reset_log()
    monkeypatch.setenv("AUTO_HEDGE_ENABLED", "true")
    monkeypatch.setenv("MAX_AUTO_FAILS_PER_MIN", "1")
    monkeypatch.setenv("HEDGE_LOG_PATH", str(tmp_path / "hedge_log.json"))

    candidate = {
        "id": "xyz",
        "symbol": "ETHUSDT",
        "long_venue": "okx-perp",
        "short_venue": "binance-um",
        "spread": 4.0,
        "spread_bps": 9.0,
        "min_spread": 3.0,
        "notional_suggestion": 500.0,
        "leverage_suggestion": 1.0,
    }

    async def fake_scan_once():
        return {"candidate": candidate, "status": "allowed"}

    monkeypatch.setattr(get_scanner(), "scan_once", fake_scan_once)

    def fake_execute(symbol, notional, leverage, min_spread):
        return {"success": False, "reason": "spread_below_threshold"}

    monkeypatch.setattr("app.auto_hedge_daemon.execute_hedged_trade", fake_execute)

    daemon = AutoHedgeDaemon()
    await daemon.run_cycle()
    await daemon.run_cycle()

    entries = read_entries()
    assert len(entries) == 2
    assert all(entry["result"].startswith("rejected: spread_below_threshold") for entry in entries)

    state = runtime.get_state()
    assert state.auto_hedge.consecutive_failures >= 2
    assert state.auto_hedge.last_execution_result.startswith("error: spread_below_threshold")
    assert runtime.is_hold_active()
    reason = runtime.get_safety_status().get("hold_reason")
    assert reason and reason.startswith("auto_hedge_failures")


@pytest.mark.asyncio
async def test_auto_daemon_simulates_in_dry_run_mode(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("AUTO_HEDGE_ENABLED", "true")
    monkeypatch.setenv("HEDGE_LOG_PATH", str(tmp_path / "hedge_log.json"))
    monkeypatch.setenv("POSITIONS_STORE_PATH", str(tmp_path / "hedge_positions.json"))

    runtime.reset_for_tests()
    runtime.record_resume_request("dry_run_mode", requested_by="pytest")
    runtime.approve_resume(actor="pytest")
    state = runtime.get_state()
    state.control.safe_mode = False
    state.control.mode = "RUN"
    reset_log()

    candidate = {
        "id": "dryrun",
        "symbol": "BTCUSDT",
        "long_venue": "binance-um",
        "short_venue": "okx-perp",
        "spread": 15.0,
        "spread_bps": 20.0,
        "min_spread": 5.0,
        "notional_suggestion": 500.0,
        "leverage_suggestion": 2.0,
    }

    async def fake_scan_once():
        return {"candidate": candidate, "status": "allowed"}

    monkeypatch.setattr(get_scanner(), "scan_once", fake_scan_once)

    class NoOrderClient:
        def __init__(self, name: str, bid: float, ask: float) -> None:
            self.name = name
            self._bid = bid
            self._ask = ask

        def get_mark_price(self, symbol: str) -> Dict[str, float]:
            mid = (self._bid + self._ask) / 2.0
            return {"symbol": symbol, "mark_price": mid}

        def get_position(self, symbol: str) -> Dict[str, float]:  # pragma: no cover - unused
            return {"symbol": symbol, "size": 0.0, "side": "flat"}

        def place_order(self, *args, **kwargs):  # pragma: no cover - should not be called
            raise AssertionError("place_order must not execute in DRY_RUN_MODE")

        def cancel_all(self, symbol: str) -> Dict[str, str]:  # pragma: no cover - unused
            return {"exchange": self.name, "symbol": symbol, "status": "skipped"}

        def get_account_limits(self) -> Dict[str, float]:  # pragma: no cover - unused
            return {"exchange": self.name, "available_balance": 0.0}

    monkeypatch.setattr(
        cross_exchange_arb,
        "_clients",
        cross_exchange_arb._ExchangeClients(
            binance=NoOrderClient("binance-um", bid=20050.0, ask=20000.0),
            okx=NoOrderClient("okx-perp", bid=20070.0, ask=20060.0),
        ),
    )

    daemon = AutoHedgeDaemon()
    await daemon.run_cycle()

    positions = list_positions()
    assert len(positions) == 1
    assert positions[0]["status"] == "simulated"
    assert positions[0].get("simulated") is True

    entries = read_entries()
    assert len(entries) == 1
    assert entries[0]["status"] == "simulated"
    assert entries[0].get("simulated") is True

    overview = get_status_overview()
    assert overview["dry_run_mode"] is True
    assert runtime.get_state().control.dry_run_mode is True
    runtime.reset_for_tests()
