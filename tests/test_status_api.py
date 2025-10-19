from __future__ import annotations


def test_status_overview(client) -> None:
    response = client.get("/api/ui/status/overview")
    payload = response.json()
    assert payload["overall"] in {"OK", "WARN", "HOLD"}
    for group in ("P0", "P1", "P2", "P3"):
        assert group in payload["scores"]
        assert 0.0 <= payload["scores"][group] <= 1.0


def test_status_components(client) -> None:
    response = client.get("/api/ui/status/components")
    payload = response.json()
    components = payload["components"]
    assert len(components) >= 20
    groups = {item["group"] for item in components}
    assert groups.issuperset({"P0", "P1", "P2", "P3"})
    for item in components:
        assert item["status"] in {"OK", "WARN", "HOLD", "ERROR"}
        assert "metrics" in item


def test_status_slo(client) -> None:
    response = client.get("/api/ui/status/slo")
    payload = response.json()
    slo = payload["slo"]
    assert set(slo).issuperset(
        {
            "ws_gap_ms_p95",
            "order_cycle_ms_p95",
            "reject_rate",
            "cancel_fail_rate",
            "recon_mismatch",
            "max_day_drawdown_bps",
            "budget_remaining",
        }
    )
    assert "thresholds" in payload
