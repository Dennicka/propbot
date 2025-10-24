from __future__ import annotations

import json
from typing import Any, Dict

import pytest

import api_cli


class StubResponse:
    def __init__(self, status_code: int, text: str, json_data: Any, headers: Dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.headers = headers or {}

    def json(self) -> Any:
        return self._json_data


def test_cli_events_csv(monkeypatch, tmp_path, capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_get(url: str, params: dict[str, Any], timeout: int) -> StubResponse:
        captured["url"] = url
        captured["params"] = params
        return StubResponse(200, "ts,venue,type,level,symbol,message\n", [{"ts": "2024"}], {"content-type": "text/csv"})

    monkeypatch.setattr(api_cli, "requests", type("R", (), {"get": staticmethod(fake_get)}))

    out_path = tmp_path / "events.csv"
    exit_code = api_cli.main([
        "--base-url",
        "http://localhost:9999",
        "events",
        "--format",
        "csv",
        "--limit",
        "50",
        "--venue",
        "binance-um",
        "--out",
        str(out_path),
    ])
    assert exit_code == 0
    assert captured["url"] == "http://localhost:9999/api/ui/events/export"
    assert captured["params"]["venue"] == "binance-um"
    assert out_path.read_text() == "ts,venue,type,level,symbol,message\n"
    stdout = capsys.readouterr().out
    assert "Events export written" in stdout


def test_cli_portfolio_json(monkeypatch, tmp_path, capsys) -> None:
    response_data = {"positions": [], "balances": []}

    def fake_get(url: str, params: dict[str, Any], timeout: int) -> StubResponse:
        return StubResponse(200, "", response_data, {"content-type": "application/json"})

    monkeypatch.setattr(api_cli, "requests", type("R", (), {"get": staticmethod(fake_get)}))

    out_path = tmp_path / "portfolio.json"
    exit_code = api_cli.main(["portfolio", "--format", "json", "--out", str(out_path)])
    assert exit_code == 0
    expected = json.dumps(response_data, indent=2, sort_keys=True) + "\n"
    assert out_path.read_text() == expected
    stdout = capsys.readouterr().out
    assert "Portfolio export written" in stdout


def test_cli_http_error(monkeypatch) -> None:
    def fake_get(url: str, params: dict[str, Any], timeout: int) -> StubResponse:
        return StubResponse(500, "boom", {"detail": "fail"}, {"content-type": "application/json"})

    monkeypatch.setattr(api_cli, "requests", type("R", (), {"get": staticmethod(fake_get)}))

    with pytest.raises(SystemExit):
        api_cli.main(["events"])
