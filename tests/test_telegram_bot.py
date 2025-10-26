from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.telebot.telegram_bot import (
    TelegramBot,
    TelegramBotConfig,
    format_status_message,
)


def test_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ENABLE", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_PUSH_MINUTES", "7")

    config = TelegramBotConfig.from_env()

    assert config.enabled is True
    assert config.token == "abc123"
    assert config.chat_id == "42"
    assert config.push_minutes == 7
    assert config.can_run is True


def test_authorization_checks_chat_id() -> None:
    config = TelegramBotConfig(token="token", chat_id="12345", enabled=True, push_minutes=5)
    bot = TelegramBot(config)

    assert bot.is_authorized_chat("12345") is True
    assert bot.is_authorized_chat(12345) is True
    assert bot.is_authorized_chat("999") is False
    assert bot.is_authorized_chat(None) is False


def test_status_formatter_contains_expected_fields() -> None:
    snapshot = SimpleNamespace(
        pnl_totals={"realized": 10.5, "unrealized": -2.25, "total": 8.25},
        positions=[SimpleNamespace(symbol="BTCUSDT", qty=0.75)],
    )
    state = SimpleNamespace(control=SimpleNamespace(safe_mode=True, environment="paper", mode="HOLD"))
    risk_state = SimpleNamespace(breaches=["risk-1"])

    message = format_status_message(snapshot, state, risk_state)

    assert "PnL=realized:10.50" in message
    assert "Positions=BTCUSDT:0.75" in message
    assert "SAFE_MODE=True" in message
    assert "PROFILE=paper" in message
    assert "RISK_BREACHES=1" in message


@pytest.mark.asyncio
async def test_status_command_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    config = TelegramBotConfig(token="token", chat_id="1", enabled=True, push_minutes=5)
    bot = TelegramBot(config)

    async def fake_status_message() -> str:
        return "Status: ok"

    monkeypatch.setattr("app.telebot.telegram_bot.build_status_message", fake_status_message)

    message = await bot._handle_status()

    assert message == "Status: ok"
