from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.utils.idem import IdempotencyCache


def test_idempotency_cache_replay() -> None:
    cache = IdempotencyCache(ttl_seconds=60)
    fingerprint = cache.build_fingerprint(
        "POST",
        "/api/ui/hold",
        b'{"foo": 1}',
        "application/json",
    )
    cache.store(
        "cache-key",
        fingerprint,
        status_code=202,
        headers=[("content-type", "application/json"), ("x-custom", "value")],
        body=b'{"detail": "ok"}',
    )
    cached = cache.get("cache-key", fingerprint)
    assert cached is not None
    assert cached.status_code == 202
    assert cached.body == b'{"detail": "ok"}'
    assert all(name.lower() != "idempotent-replay" for name, _ in cached.headers)


def test_duplicate_post_returns_cached_response(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    hold_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.ui.hold_loop", hold_mock)

    first = client.post("/api/ui/hold", headers={"Idempotency-Key": "same-key"})
    assert first.status_code == 200
    assert hold_mock.await_count == 1

    replay = client.post("/api/ui/hold", headers={"Idempotency-Key": "same-key"})
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert hold_mock.await_count == 1
