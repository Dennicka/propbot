import pytest

from app.services import cache as ui_cache
from app.metrics.cache import reset_cache_metrics


@pytest.fixture(autouse=True)
def reset_cache_state():
    ui_cache.clear()
    reset_cache_metrics()
    yield
    ui_cache.clear()
    reset_cache_metrics()


def test_status_cache_hit_and_metrics(monkeypatch, client):
    call_count = {"value": 0}

    def fake_overview():
        call_count["value"] += 1
        return {"overall": "OK", "calls": call_count["value"]}

    monkeypatch.setattr("app.routers.ui_status.get_status_overview", fake_overview)

    first = client.get("/api/ui/status/overview")
    second = client.get("/api/ui/status/overview")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert call_count["value"] == 1

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "cache_hit_ratio" in metrics.text
    assert 'cache_hit_ratio{endpoint="/api/ui/status/overview"} 0.5' in metrics.text


def test_status_cache_ttl_expiration(monkeypatch, client):
    call_count = {"value": 0}

    def fake_overview():
        call_count["value"] += 1
        return {"value": call_count["value"]}

    times = {"now": 1000.0}

    def fake_monotonic():
        return times["now"]

    monkeypatch.setattr("app.routers.ui_status.get_status_overview", fake_overview)
    monkeypatch.setattr("app.services.cache._monotonic", fake_monotonic)

    first = client.get("/api/ui/status/overview")
    assert first.status_code == 200
    assert first.json()["value"] == 1
    assert call_count["value"] == 1

    times["now"] += 0.5
    second = client.get("/api/ui/status/overview")
    assert second.status_code == 200
    assert second.json()["value"] == 1
    assert call_count["value"] == 1

    times["now"] += 2.0
    third = client.get("/api/ui/status/overview")
    assert third.status_code == 200
    assert third.json()["value"] == 2
    assert call_count["value"] == 2


def test_dashboard_etag(monkeypatch, client):
    response = client.get("/ui/dashboard")
    assert response.status_code == 200
    etag = response.headers.get("ETag")
    last_modified = response.headers.get("Last-Modified")
    cache_control = response.headers.get("Cache-Control")

    assert etag
    assert last_modified
    assert cache_control == "no-cache, must-revalidate"

    conditional = client.get(
        "/ui/dashboard",
        headers={"If-None-Match": etag},
    )
    assert conditional.status_code == 304
    assert conditional.headers.get("ETag") == etag
    assert conditional.headers.get("Last-Modified") == last_modified


def test_static_assets_etag(client):
    response = client.get("/static/dashboard.css")
    assert response.status_code == 200
    etag = response.headers.get("ETag")
    last_modified = response.headers.get("Last-Modified")
    cache_control = response.headers.get("Cache-Control")

    assert etag
    assert last_modified
    assert cache_control is not None and "immutable" in cache_control

    conditional = client.get(
        "/static/dashboard.css",
        headers={"If-None-Match": etag},
    )
    assert conditional.status_code == 304
    assert conditional.headers.get("ETag") == etag
    assert conditional.headers.get("Last-Modified") == last_modified
