import asyncio
from types import SimpleNamespace

import pytest

from opsbot.notify import notify_ops
from opsbot.telegram_bot import (
    OpsTelegramBot,
    OpsTelegramBotConfig,
    build_status_report,
)


@pytest.mark.asyncio
async def test_status_report_compiles(monkeypatch: pytest.MonkeyPatch) -> None:
    class Snapshot(SimpleNamespace):
        positions = [
            SimpleNamespace(symbol="BTCUSDT", notional=1000.0, leverage=2.0),
        ]

    async def fake_snapshot() -> Snapshot:
        return Snapshot()

    monkeypatch.setattr("app.services.portfolio.snapshot", fake_snapshot)
    monkeypatch.setattr("app.ledger.fetch_open_orders", lambda: [])
    monkeypatch.setattr("app.services.runtime.set_open_orders", lambda orders: None)
    monkeypatch.setattr(
        "app.services.runtime.get_state",
        lambda: SimpleNamespace(control=SimpleNamespace(safe_mode=True, mode="HOLD")),
    )
    monkeypatch.setattr(
        "app.services.runtime.get_safety_status",
        lambda: {"hold_active": True, "hold_reason": "testing"},
    )
    monkeypatch.setattr(
        "app.services.risk.refresh_runtime_state",
        lambda snapshot, open_orders: SimpleNamespace(breaches=[]),
    )
    monkeypatch.setattr(
        "positions.list_open_positions",
        lambda: [
            {
                "symbol": "ETHUSDT",
                "long_venue": "binance", 
                "short_venue": "okx",
                "notional_usdt": 5000.0,
                "leverage": 3.0,
                "entry_spread_bps": 12.5,
            }
        ],
    )

    report = await build_status_report()

    assert "SAFE_MODE=True" in report
    assert "HOLD_ACTIVE=True" in report
    assert "ETHUSDT" in report


def test_notify_ops_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("OPS_ALERTS_PATH", str(tmp_path / "ops_alerts.json"))
    record = notify_ops("TEST_EVENT", {"symbol": "BTCUSDT", "reason": "unit"})
    assert record["event"] == "TEST_EVENT"
    assert (tmp_path / "ops_alerts.json").exists()


@pytest.mark.asyncio
async def test_resume_confirm_token_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    config = OpsTelegramBotConfig(token="t", chat_id="1", enabled=True, two_man_token="secret")
    bot = OpsTelegramBot(config)

    monkeypatch.setattr("app.services.runtime.is_hold_active", lambda: True)
    safety_state = {"resume_request": {"pending": True, "reason": "maintenance"}}
    monkeypatch.setattr("app.services.runtime.get_safety_status", lambda: safety_state)

    events: list[tuple[str, dict]] = []

    def fake_notify(event: str, details):
        events.append((event, details))
        return {"event": event, "details": details}

    monkeypatch.setattr("opsbot.telegram_bot.notify_ops", fake_notify)

    result = await bot.process_resume_confirm(token="wrong", note="")
    assert result == "Invalid approval token."
    assert events and events[-1][0] == "RESUME CONFIRM DENIED"

    monkeypatch.setattr("app.services.runtime.approve_resume", lambda actor=None: {"hold_cleared": True})
    events.clear()
    result_ok = await bot.process_resume_confirm(token="secret", note="looks good")
    assert result_ok == "Resume confirmed."
    assert events and events[-1][0] == "RESUME CONFIRMED"
