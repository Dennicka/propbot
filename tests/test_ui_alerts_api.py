from datetime import datetime, timezone

from app.alerts.registry import OpsAlert, OpsAlertsRegistry


def _populate_registry() -> OpsAlertsRegistry:
    registry = OpsAlertsRegistry(maxlen=10)
    registry.add(
        OpsAlert(
            ts=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            event_type="alpha",
            message="alpha-one",
            severity="info",
            source="test-suite",
            context={"seq": 1},
        )
    )
    registry.add(
        OpsAlert(
            ts=datetime(2024, 1, 1, 12, 5, tzinfo=timezone.utc),
            event_type="beta",
            message="beta-critical",
            severity="critical",
            source="test-suite",
            profile="paper",
            context={"seq": 2},
        )
    )
    registry.add(
        OpsAlert(
            ts=datetime(2024, 1, 1, 12, 10, tzinfo=timezone.utc),
            event_type="alpha",
            message="alpha-warning",
            severity="warning",
            source="test-suite",
            context={"seq": 3},
        )
    )
    return registry


def test_alerts_endpoint_returns_recent_alerts(monkeypatch, client) -> None:
    registry = _populate_registry()
    monkeypatch.setattr("app.services.runtime.get_ops_alerts_registry", lambda: registry)

    response = client.get("/api/ui/alerts", params={"limit": 2})
    assert response.status_code == 200
    payload = response.json()

    assert isinstance(payload, list)
    assert [item["message"] for item in payload] == ["alpha-warning", "beta-critical"]
    assert payload[0]["context"] == {"seq": 3}
    assert payload[1]["profile"] == "paper"


def test_alerts_endpoint_filters_by_event_type(monkeypatch, client) -> None:
    registry = _populate_registry()
    monkeypatch.setattr("app.services.runtime.get_ops_alerts_registry", lambda: registry)

    response = client.get("/api/ui/alerts", params={"event_type": "alpha"})
    assert response.status_code == 200

    payload = response.json()
    assert len(payload) == 2
    assert {item["event_type"] for item in payload} == {"alpha"}


def test_alerts_endpoint_filters_by_severity(monkeypatch, client) -> None:
    registry = _populate_registry()
    monkeypatch.setattr("app.services.runtime.get_ops_alerts_registry", lambda: registry)

    response = client.get("/api/ui/alerts", params={"severity": "critical"})
    assert response.status_code == 200

    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["severity"] == "critical"
    assert payload[0]["message"] == "beta-critical"
