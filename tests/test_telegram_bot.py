from __future__ import annotations

from types import SimpleNamespace

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
    state = SimpleNamespace(
        control=SimpleNamespace(
            safe_mode=True,
            environment="paper",
            mode="HOLD",
            dry_run_mode=True,
            dry_run=True,
        )
    )
    risk_state = SimpleNamespace(breaches=["risk-1"])
    safety = {"hold_active": True, "hold_reason": "unit_test"}
    auto_state = SimpleNamespace(enabled=True, last_execution_ts="2024-01-01T00:00:00Z", consecutive_failures=2)
    pending = [{"action": "resume", "id": "abc12345"}]

    message = format_status_message(snapshot, state, risk_state, safety, auto_state, pending)

    assert "PnL=realized:10.50" in message
    assert "Positions=BTCUSDT:0.75" in message
    assert "SAFE_MODE=True" in message
    assert "PROFILE=paper" in message
    assert "RISK_BREACHES=1" in message
    assert "HOLD_ACTIVE=True" in message
    assert "DRY_RUN_MODE=True" in message
    assert "AUTO_HEDGE=on" in message
    assert "Pending approvals=resume:abc12345" in message


@pytest.mark.asyncio
async def test_status_command_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    config = TelegramBotConfig(token="token", chat_id="1", enabled=True, push_minutes=5)
    bot = TelegramBot(config)

    async def fake_status_message() -> str:
        return "Status: ok"

    monkeypatch.setattr("app.telebot.telegram_bot.build_status_message", fake_status_message)

    message = await bot._handle_status()

    assert message == "Status: ok"


@pytest.mark.asyncio
async def test_daily_command_formats_report(monkeypatch: pytest.MonkeyPatch) -> None:
    config = TelegramBotConfig(token="token", chat_id="1", enabled=True, push_minutes=5)
    bot = TelegramBot(config)

    sample_report = {
        "timestamp": "2024-05-01T00:00:00+00:00",
        "window_hours": 24,
        "pnl_realized_total": 12.5,
        "pnl_unrealized_avg": 4.2,
        "exposure_avg": 500.0,
        "slippage_avg_bps": 0.75,
        "hold_events": 3,
        "hold_breakdown": {"safety_hold": 2, "risk_throttle": 1},
        "pnl_unrealized_samples": 4,
        "exposure_samples": 4,
        "slippage_samples": 2,
    }

    monkeypatch.setattr("app.telebot.telegram_bot.load_latest_report", lambda: sample_report)

    message = await bot._handle_daily()

    assert "Daily report:" in message
    assert "PnL_realized=12.50" in message
    assert "HOLD_events=3" in message


@pytest.mark.asyncio
async def test_daily_command_handles_missing_report(monkeypatch: pytest.MonkeyPatch) -> None:
    config = TelegramBotConfig(token="token", chat_id="1", enabled=True, push_minutes=5)
    bot = TelegramBot(config)

    monkeypatch.setattr("app.telebot.telegram_bot.load_latest_report", lambda: None)

    message = await bot._handle_daily()

    assert "No daily report" in message
