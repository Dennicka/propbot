from __future__ import annotations

from typing import Any

from app.alerts.registry import REGISTRY


def _make_alert(index: int, **overrides: Any) -> None:
    REGISTRY.record(
        level=overrides.get("level", "WARN"),
        source=overrides.get("source", "test"),
        message=overrides.get("message", f"alert-{index}"),
        code=overrides.get("code"),
        details=overrides.get("details"),
        ts=overrides.get("ts", float(index)),
    )


def test_ui_alerts_endpoint_returns_recent_items(client) -> None:
    REGISTRY.clear()
    try:
        for idx in range(3):
            _make_alert(idx, ts=100.0 + idx, details={"seq": idx})

        response = client.get("/api/ui/alerts")
        assert response.status_code == 200

        payload = response.json()
        items = payload.get("items")
        assert isinstance(items, list)
        assert len(items) == 3

        for expected_idx, item in enumerate(items):
            assert {"ts", "level", "source", "message"}.issubset(item)
            assert item["message"] == f"alert-{expected_idx}"
            assert item.get("details", {}).get("seq") == expected_idx
    finally:
        REGISTRY.clear()


def test_ui_alerts_endpoint_limit_parameter(client) -> None:
    REGISTRY.clear()
    try:
        for idx in range(10):
            _make_alert(idx, ts=200.0 + idx)

        response = client.get("/api/ui/alerts", params={"limit": 2})
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 2
        assert [item["message"] for item in items] == ["alert-8", "alert-9"]

        for idx in range(10, 215):
            _make_alert(idx, ts=200.0 + idx)

        response = client.get("/api/ui/alerts", params={"limit": 999})
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 200
        assert items[-1]["message"] == "alert-214"
    finally:
        REGISTRY.clear()
