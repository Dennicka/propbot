from __future__ import annotations

from app.services.runtime import get_state, register_approval, set_preflight_result


def test_live_readiness_paper_mode(client) -> None:
    payload = client.get("/live-readiness").json()
    assert payload["s"] == "READY"
    assert payload["safe_mode"] is True


def test_live_readiness_requires_approvals_when_live(client) -> None:
    state = get_state()
    state.control.safe_mode = False
    set_preflight_result(True)
    payload = client.get("/live-readiness").json()
    assert payload["s"] == "HOLD"
    register_approval("operator_a", "ok")
    register_approval("operator_b", "ok")
    payload = client.get("/live-readiness").json()
    assert payload["s"] == "READY"
