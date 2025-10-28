from datetime import datetime, timezone

from app.services import risk_guard, runtime
from app.services.runtime import is_hold_active
from app.runtime_state_store import load_runtime_payload
from app.opsbot import notifier
from positions import create_position, reset_positions


def test_risk_guard_force_hold_on_runaway_notional(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(tmp_path / "runtime_state.json"))
    monkeypatch.setenv("POSITIONS_STORE_PATH", str(tmp_path / "positions.json"))
    monkeypatch.setenv("OPS_ALERTS_FILE", str(tmp_path / "ops_alerts.json"))
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "1000")

    runtime.reset_for_tests()
    reset_positions()

    runtime.record_resume_request("prep", requested_by="pytest")
    runtime.approve_resume(actor="pytest")
    assert is_hold_active() is False

    create_position(
        symbol="ETHUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=800.0,
        entry_spread_bps=10.0,
        leverage=2.0,
    )
    create_position(
        symbol="BTCUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=400.0,
        entry_spread_bps=8.0,
        leverage=1.5,
    )

    triggered = risk_guard.evaluate(now=datetime.now(timezone.utc))

    assert risk_guard.REASON_RUNAWAY_NOTIONAL in triggered
    assert is_hold_active() is True

    payload = load_runtime_payload()
    safety = payload.get("safety", {})
    assert safety.get("hold_reason") == risk_guard.REASON_RUNAWAY_NOTIONAL

    alerts = notifier.get_recent_alerts(limit=5)
    kinds = [entry.get("kind") for entry in alerts]
    assert "risk_guard_force_hold" in kinds
    last = next(entry for entry in alerts if entry.get("kind") == "risk_guard_force_hold")
    assert last.get("extra", {}).get("reason") == risk_guard.REASON_RUNAWAY_NOTIONAL

