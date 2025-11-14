from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.alerts.levels import AlertLevel
from app.alerts.manager import notify


@pytest.fixture(autouse=True)
def _clear_alerts_env(monkeypatch, tmp_path) -> None:
    log_path = tmp_path / "alerts.log"
    monkeypatch.setenv("ALERTS_FILE_PATH", str(log_path))
    monkeypatch.setenv("ALERTS_TG_BOT_TOKEN", "token")
    monkeypatch.setenv("ALERTS_TG_CHAT_ID", "chat")
    # ensure file path directory exists before tests assert
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)


def test_paper_profile_routes_only_critical(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_PROFILE", "paper")
    mock_send = Mock(return_value=200)
    monkeypatch.setattr("app.alerts.manager.wire_telegram.send_message", mock_send)

    notify(AlertLevel.INFO, "info", profile="paper")
    notify(AlertLevel.WARN, "warn", profile="paper")
    notify(AlertLevel.CRITICAL, "critical", profile="paper")

    assert mock_send.call_count == 1


def test_testnet_routes_warn_and_above(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_PROFILE", "testnet")
    mock_send = Mock(return_value=200)
    monkeypatch.setattr("app.alerts.manager.wire_telegram.send_message", mock_send)

    notify(AlertLevel.INFO, "info", profile="testnet")
    assert mock_send.call_count == 0

    notify(AlertLevel.WARN, "warn", profile="testnet")
    notify(AlertLevel.ERROR, "error", profile="testnet")
    notify(AlertLevel.CRITICAL, "critical", profile="testnet")

    assert mock_send.call_count == 3


def test_live_profile_matches_testnet(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_PROFILE", "live")
    mock_send = Mock(return_value=200)
    monkeypatch.setattr("app.alerts.manager.wire_telegram.send_message", mock_send)

    notify(AlertLevel.WARN, "warn", profile="live")
    notify(AlertLevel.CRITICAL, "critical", profile="live")

    assert mock_send.call_count == 2
