from __future__ import annotations

from app.services import arbitrage
from app.services.runtime import get_state, update_guard


GUARDS = [
    "cancel_on_disconnect",
    "rate_limit",
    "clock_skew",
    "snapshot_diff",
    "kill_caps",
    "runaway_breaker",
    "maintenance_calendar",
]


def _overview(client) -> str:
    return client.get("/api/ui/status/overview").json()["overall"]


def test_guardrails_toggle_updates_status(client) -> None:
    arbitrage.run_preflight()
    base = _overview(client)
    for guard in GUARDS:
        update_guard(guard, "WARN", "test")
        state_after = _overview(client)
        assert state_after in {"WARN", "HOLD"} or state_after != base
        ctrl = client.get("/api/ui/control-state").json()
        assert ctrl["guards"][guard] == "WARN"
        update_guard(guard, "OK", "reset")
    restored = _overview(client)
    assert restored == base


def test_rescue_triggers_guard_and_incident(client) -> None:
    arbitrage.run_preflight()
    result = arbitrage.execute_trade(None, 0.01, force_leg_b_fail=True)
    assert result["state"] == "HEDGE_OUT"
    ctrl = client.get("/api/ui/control-state").json()
    assert ctrl["guards"]["runaway_breaker"] in {"WARN", "HOLD"}
    incidents = get_state().incidents
    assert any(item["kind"] == "hedge" for item in incidents)
