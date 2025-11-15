from fastapi.testclient import TestClient

from app.router.sor_log import (
    append_router_decision,
    make_log_entry,
    reset_router_decisions_for_tests,
)


def test_router_decisions_endpoint_smoke(client: TestClient) -> None:
    reset_router_decisions_for_tests()
    append_router_decision(
        make_log_entry(
            symbol="BTCUSDT",
            strategy_id="test-strategy",
            runtime_profile="paper",
            candidates=[],
            chosen=None,
        )
    )

    response = client.get("/api/ui/router-decisions?limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert payload

    entry = payload[0]
    assert entry["symbol"] == "BTCUSDT"
    assert entry["runtime_profile"] == "paper"
    assert entry["reject_reason"] == "no_venue_selected"
    assert entry["candidates"] == []
