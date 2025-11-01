from datetime import datetime, timezone

from app import ledger
from app.risk.runaway_guard import get_guard
from app.services import runtime
from app.services.runtime import (
    approve_resume,
    get_safety_status,
    record_resume_request,
    set_mode,
    update_runaway_guard_snapshot,
)


def test_runaway_guard_blocks_cancel_all(client, monkeypatch):
    monkeypatch.setenv("FEATURE_RUNAWAY_GUARD_V2", "1")
    runtime.reset_for_tests()
    state = runtime.get_state()
    state.control.environment = "testnet"
    state.control.safe_mode = False
    state.safety.clear_hold()
    set_mode("RUN")
    ledger.reset()
    guard = get_guard()
    guard.configure(max_cancels_per_min=2, cooldown_sec=30)
    update_runaway_guard_snapshot(guard.snapshot())
    now = datetime.now(timezone.utc).isoformat()
    for idx in range(3):
        ledger.record_order(
            venue="binance-um",
            symbol="BTCUSDT",
            side="buy",
            qty=0.1,
            price=25000.0,
            status="submitted",
            client_ts=now,
            exchange_ts=None,
            idemp_key=f"runaway-test-{idx}",
        )
    first = client.post("/api/ui/cancel_all")
    assert first.status_code == 423
    detail = first.json().get("detail")
    assert detail
    safety = get_safety_status()
    assert safety.get("hold_active") is True
    assert "runaway_guard_v2" in str(safety.get("hold_reason"))
    events = ledger.fetch_events(limit=5)
    assert any(event.get("code") == "cancel_all_blocked_runaway" for event in events)
    request = record_resume_request("clear runaway guard", requested_by="pytest")
    approve_resume(request_id=request.get("id"), actor="pytest")
    safety = get_safety_status()
    assert safety.get("hold_active") is False
    second = client.post("/api/ui/cancel_all")
    assert second.status_code == 403
    cooldown_detail = second.json().get("detail")
    assert cooldown_detail.get("error") == "runaway_guard_cooldown"
    guard_snapshot = get_safety_status().get("runaway_guard", {})
    last_block = guard_snapshot.get("last_block") if isinstance(guard_snapshot, dict) else None
    assert last_block
    assert last_block.get("reason") in {"limit_exceeded", "cooldown_active"}
