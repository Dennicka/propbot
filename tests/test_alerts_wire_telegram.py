from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

import pytest


def _reload_wire(monkeypatch: pytest.MonkeyPatch, base_url: str) -> ModuleType:
    monkeypatch.setenv("TELEGRAM_API_BASE", base_url)
    import app.alerts.wire_telegram as wire

    return importlib.reload(wire)


def test_send_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = _reload_wire(monkeypatch, "https://api.telegram.org")

    called: dict[str, Any] = {}

    def fake_post(url: str, data: dict[str, Any], timeout: float) -> Any:
        called["url"] = url
        called["data"] = data
        called["timeout"] = timeout

        class Response:
            status_code = 200
            text = "ok"

        return Response()

    monkeypatch.setattr("app.alerts.wire_telegram.requests.post", fake_post)

    status = wire.send_message("token", "123", "hello", timeout=2.5)

    assert status == 200
    assert called["url"].startswith("https://api.telegram.org/bottoken/sendMessage")
    assert called["data"]["chat_id"] == "123"
    assert called["data"]["parse_mode"] == "HTML"
    assert called["timeout"] == 2.5


def test_disallow_http_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = _reload_wire(monkeypatch, "http://api.telegram.org")

    with pytest.raises(wire.TelegramWireError):
        wire.send_message("token", "123", "hello")


def test_disallow_host(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = _reload_wire(monkeypatch, "https://evil.example.com")

    with pytest.raises(wire.TelegramWireError):
        wire.send_message("token", "123", "hello")
