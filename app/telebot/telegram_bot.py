from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable

import httpx
from fastapi import FastAPI

from .. import ledger
from ..services import portfolio, risk
from ..services.loop import cancel_all_orders, hold_loop, resume_loop
from ..services.runtime import get_state, set_mode, set_open_orders

LOGGER = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(1, value)


@dataclass(frozen=True)
class TelegramBotConfig:
    token: str | None
    chat_id: str | None
    enabled: bool
    push_minutes: int

    @classmethod
    def from_env(cls) -> "TelegramBotConfig":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        enabled = _env_flag("TELEGRAM_ENABLE", False)
        push_minutes = _env_int("TELEGRAM_PUSH_MINUTES", 5)
        return cls(token=token, chat_id=chat_id, enabled=enabled, push_minutes=push_minutes)

    @property
    def can_run(self) -> bool:
        return self.enabled and bool(self.token) and bool(self.chat_id)


def _normalise_symbol(value: Any) -> str:
    if isinstance(value, str):
        return value.upper()
    return str(value or "").upper()


def _extract_attr(item: Any, key: str) -> Any:
    if hasattr(item, key):
        return getattr(item, key)
    if isinstance(item, dict):
        return item.get(key)
    return None


def _summarise_positions(positions: Iterable[Any]) -> str:
    entries = []
    for position in positions:
        symbol = _normalise_symbol(_extract_attr(position, "symbol"))
        qty = _extract_attr(position, "qty")
        if qty is None:
            qty = _extract_attr(position, "notional")
        try:
            qty_value = float(qty)
        except (TypeError, ValueError):
            qty_value = 0.0
        if abs(qty_value) <= 1e-12:
            continue
        entries.append(f"{symbol}:{qty_value:.4g}")
    if not entries:
        return "none"
    if len(entries) > 5:
        head = ", ".join(entries[:5])
        return f"{head}, +{len(entries) - 5} more"
    return ", ".join(entries)


def format_status_message(snapshot: Any, state: Any, risk_state: Any | None) -> str:
    pnl_totals = getattr(snapshot, "pnl_totals", {}) or {}
    realized = float(pnl_totals.get("realized", 0.0))
    unrealized = float(pnl_totals.get("unrealized", 0.0))
    total = float(pnl_totals.get("total", realized + unrealized))
    positions = getattr(snapshot, "positions", []) or []
    positions_summary = _summarise_positions(positions)
    control = getattr(state, "control", state)
    safe_mode = bool(getattr(control, "safe_mode", False))
    profile = str(getattr(control, "environment", "paper") or "paper").lower()
    mode = str(getattr(control, "mode", "HOLD")).upper()
    breaches = []
    if risk_state is not None:
        breaches = list(getattr(risk_state, "breaches", []) or [])
    breaches_summary = f"RISK_BREACHES={len(breaches)}"
    lines = [
        "Status:",
        f"PnL=realized:{realized:.2f}, unrealized:{unrealized:.2f}, total:{total:.2f}",
        f"Positions={positions_summary}",
        f"SAFE_MODE={safe_mode}",
        f"MODE={mode}",
        f"PROFILE={profile}",
        breaches_summary,
    ]
    return "\n".join(lines)


async def build_status_message() -> str:
    snapshot = await portfolio.snapshot()
    open_orders = await asyncio.to_thread(ledger.fetch_open_orders)
    set_open_orders(open_orders)
    state = get_state()
    risk_state = risk.refresh_runtime_state(snapshot=snapshot, open_orders=open_orders)
    return format_status_message(snapshot, state, risk_state)


class TelegramBot:
    def __init__(self, config: TelegramBotConfig):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop_event = asyncio.Event()
        self._update_offset = 0
        self.logger = LOGGER

    def is_authorized_chat(self, chat_id: Any) -> bool:
        if chat_id is None:
            return False
        return str(chat_id) == str(self.config.chat_id)

    async def start(self) -> None:
        if not self.config.can_run:
            self.logger.info("Telegram bot disabled or missing credentials; skipping startup")
            return
        if self._client is not None:
            return
        self.logger.info("Starting Telegram bot background tasks")
        base_url = f"https://api.telegram.org/bot{self.config.token}/"
        self._client = httpx.AsyncClient(base_url=base_url, timeout=20.0)
        await self._discard_pending_updates()
        self._stop_event.clear()
        status_task = asyncio.create_task(self._status_loop(), name="telegram-status-loop")
        command_task = asyncio.create_task(self._command_loop(), name="telegram-command-loop")
        self._tasks = [status_task, command_task]

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                continue
            except Exception:  # pragma: no cover - defensive logging
                self.logger.exception("Telegram task exited with error")
        self._tasks.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.logger.info("Telegram bot stopped")

    async def send_message(self, text: str) -> None:
        if not self.config.can_run:
            return
        if self._client is None:
            self.logger.warning("Telegram client not initialized; message skipped")
            return
        try:
            response = await self._client.post("sendMessage", json={"chat_id": self.config.chat_id, "text": text})
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover - network failures are logged
            self.logger.warning("Failed to send Telegram message", extra={"error": str(exc)})

    async def send_status_update(self) -> None:
        if not self.config.can_run:
            return
        message = await build_status_message()
        await self.send_message(message)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    async def _status_loop(self) -> None:
        interval = max(1, int(self.config.push_minutes)) * 60
        while not self._stop_event.is_set():
            try:
                await self.send_status_update()
            except Exception:  # pragma: no cover - logged for observability
                self.logger.exception("Failed to push Telegram status update")
            await self._wait(interval)

    async def _command_loop(self) -> None:
        if self._client is None:
            return
        while not self._stop_event.is_set():
            try:
                payload = await self._client.get(
                    "getUpdates",
                    params={"offset": self._update_offset, "timeout": 30, "allowed_updates": ["message"]},
                )
                payload.raise_for_status()
                data = payload.json()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - log and retry
                self.logger.warning("Telegram update polling failed", extra={"error": str(exc)})
                await self._wait(5)
                continue
            updates = data.get("result", []) if isinstance(data, dict) else []
            if not isinstance(updates, list):
                updates = []
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._update_offset = update_id + 1
                try:
                    await self._handle_update(update)
                except Exception:  # pragma: no cover - ensure loop keeps running
                    self.logger.exception("Failed to handle Telegram update")

    async def _discard_pending_updates(self) -> None:
        if self._client is None:
            return
        try:
            response = await self._client.get("getUpdates", params={"timeout": 0})
            response.raise_for_status()
            data = response.json()
            updates = data.get("result", []) if isinstance(data, dict) else []
            if updates:
                last_id = max(update.get("update_id", 0) for update in updates if isinstance(update, dict))
                if isinstance(last_id, int) and last_id:
                    self._update_offset = last_id + 1
        except Exception:  # pragma: no cover - best effort priming
            self.logger.debug("Failed to discard pending Telegram updates", exc_info=True)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        chat_id = None
        if isinstance(chat, dict):
            chat_id = chat.get("id")
        if not self.is_authorized_chat(chat_id):
            self.logger.warning("Ignoring Telegram message from unauthorized chat", extra={"chat_id": chat_id})
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        text = text.strip()
        if not text:
            return
        command = text.split()[0].lower()
        if "@" in command:
            command = command.split("@", 1)[0]
        if not command:
            return
        try:
            if command == "/pause":
                response = await self._handle_pause()
            elif command == "/resume":
                response = await self._handle_resume()
            elif command in {"/close_all", "/closeall"}:
                response = await self._handle_close_all()
            else:
                response = "Unknown command. Available: /pause, /resume, /close_all"
        except Exception as exc:  # pragma: no cover - ensure failure is reported
            self.logger.exception("Telegram command failed", extra={"command": command})
            response = f"Command failed: {exc}"[:400]
        await self.send_message(response)

    async def _handle_pause(self) -> str:
        state = get_state()
        state.control.safe_mode = True
        await hold_loop()
        set_mode("HOLD")
        ledger.record_event(level="INFO", code="mode_change", payload={"mode": "HOLD", "source": "telegram"})
        return "Trading paused. SAFE_MODE=True."

    async def _handle_resume(self) -> str:
        state = get_state()
        state.control.safe_mode = False
        await resume_loop()
        set_mode("RUN")
        ledger.record_event(level="INFO", code="mode_change", payload={"mode": "RUN", "source": "telegram"})
        return "Trading resumed. SAFE_MODE=False."

    async def _handle_close_all(self) -> str:
        state = get_state()
        environment = str(getattr(state.control, "environment", "")).lower()
        if environment != "testnet":
            return "Cancel-all only available on testnet profile."
        result = await cancel_all_orders()
        ledger.record_event(level="INFO", code="cancel_all", payload={"source": "telegram", **result})
        return "Cancel-all requested."


def setup_telegram_bot(app: FastAPI) -> None:
    config = TelegramBotConfig.from_env()
    bot = TelegramBot(config)
    app.state.telegram_bot = bot

    @app.on_event("startup")
    async def _start_bot() -> None:  # pragma: no cover - exercised in integration environments
        await bot.start()

    @app.on_event("shutdown")
    async def _stop_bot() -> None:  # pragma: no cover - exercised in integration environments
        await bot.stop()
