from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import app


class _FakeClock:
    def __init__(self) -> None:
        self.current = 0.0

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


def test_rate_limit_exceeds_burst(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    hold_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.ui.hold_loop", hold_mock)

    fake_clock = _FakeClock()
    limiter = app.state.rate_limiter
    limiter.set_clock(fake_clock)
    limiter.set_limits(rate_per_min=6, burst=2)

    first = client.post("/api/ui/hold")
    second = client.post("/api/ui/hold")
    assert first.status_code == 200
    assert second.status_code == 200

    third = client.post("/api/ui/hold")
    assert third.status_code == 429
    assert third.json() == {"detail": "rate limit exceeded"}
    assert third.headers["X-RateLimit-Remaining"] == "0"
    assert "X-RateLimit-Reset" in third.headers

    fake_clock.advance(11.0)
    fourth = client.post("/api/ui/hold")
    assert fourth.status_code == 200
    assert hold_mock.await_count == 3
    assert int(fourth.headers["X-RateLimit-Remaining"]) <= 1
