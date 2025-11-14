from __future__ import annotations

import urllib.error
import urllib.parse

import pytest

from app.alerts import wire_telegram


class DummyResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> "DummyResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_send_message_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout: int):
        captured["timeout"] = timeout
        captured["data"] = request.data
        captured["headers"] = dict(request.header_items())
        return DummyResponse(status=200)

    monkeypatch.setattr(wire_telegram.urllib.request, "urlopen", fake_urlopen)
    result = wire_telegram.send_message(
        token="TOKEN",
        chat_id="123",
        text="hello",
        timeout=5,
        retries=[],
    )
    assert result is True
    payload = urllib.parse.parse_qs(captured["data"].decode("utf-8"))
    assert payload["chat_id"] == ["123"]
    assert payload["text"] == ["hello"]
    assert payload["parse_mode"] == ["Markdown"]
    assert captured["headers"]["Content-type"] == "application/x-www-form-urlencoded"


def test_send_message_retries_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    errors = [
        urllib.error.URLError("timeout"),
        urllib.error.HTTPError(
            "https://api.telegram.org/botTOKEN/sendMessage", 502, "bad", hdrs=None, fp=None
        ),
        urllib.error.HTTPError(
            "https://api.telegram.org/botTOKEN/sendMessage", 500, "bad", hdrs=None, fp=None
        ),
    ]
    sleeps: list[int] = []

    def fake_urlopen(request, timeout: int):
        raise errors.pop(0)

    monkeypatch.setattr(wire_telegram.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(wire_telegram.time, "sleep", sleeps.append)
    result = wire_telegram.send_message(
        token="TOKEN",
        chat_id="123",
        text="hello",
        timeout=5,
        retries=[1, 2],
    )
    assert result is False
    assert sleeps == [1, 2]
    assert not errors
