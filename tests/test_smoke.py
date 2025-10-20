from __future__ import annotations

from typing import Iterable


def _assert_all_ok(client, endpoints: Iterable[str]) -> None:
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200, endpoint


def test_smoke_endpoints(client) -> None:
    get_endpoints = [
        "/api/health",
        "/openapi.json",
        "/metrics",
        "/metrics/latency",
        "/live-readiness",
        "/api/opportunities",
        "/api/ui/status/overview",
        "/api/ui/status/components",
        "/api/ui/status/slo",
        "/api/ui/control-state",
        "/api/ui/state",
        "/api/ui/execution",
        "/api/ui/pnl",
        "/api/ui/exposure",
        "/api/ui/limits",
        "/api/ui/universe",
        "/api/ui/approvals",
        "/api/ui/recon/status",
        "/api/ui/recon/history",
        "/api/deriv/status",
        "/api/deriv/positions",
        "/api/arb/edge",
    ]
    _assert_all_ok(client, get_endpoints)

    ui_state = client.get("/api/ui/state").json()
    assert "flags" in ui_state
    for key in ["MODE", "SAFE_MODE", "POST_ONLY", "REDUCE_ONLY", "ENV"]:
        assert key in ui_state["flags"]

    preview = client.post("/api/arb/preview", json={})
    assert preview.status_code == 200
    assert "preflight" in preview.json()

    recon_run = client.post("/api/ui/recon/run")
    assert recon_run.status_code == 200
    assert recon_run.json()["ok"] is True
