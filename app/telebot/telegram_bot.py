from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, TYPE_CHECKING

from fastapi import FastAPI

from .. import ledger
from ..services import portfolio, risk
from ..services.loop import cancel_all_orders, hold_loop, resume_loop
from ..services.runtime import get_state, set_mode, set_open_orders

if TYPE_CHECKING:  # pragma: no cover - import heavy dependencies only for type checking
    from telegram import Update
    from telegram.ext import Application


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
        self._application: Application | None = None
        self._polling_task: asyncio.Task[Any] | None = None
        self.logger = LOGGER

    def is_authorized_chat(self, chat_id: Any) -> bool:
        if chat_id is None:
            return False
        return str(chat_id) == str(self.config.chat_id)

    def _load_ptb(self) -> tuple[Any, ...]:
        try:
            from telegram.ext import AIORateLimiter, ApplicationBuilder, CommandHandler, MessageHandler, filters
        except ModuleNotFoundError as exc:  # pragma: no cover - surfaced in CI when optional dependency missing
            raise RuntimeError("python-telegram-bot is not installed") from exc
        return AIORateLimiter, ApplicationBuilder, CommandHandler, MessageHandler, filters

    def _command_wrapper(
        self, handler: Callable[[], Awaitable[str]]
    ) -> Callable[["Update", Any], Awaitable[None]]:
        async def _wrapped(update: "Update", context: Any) -> None:
            chat = update.effective_chat
            chat_id = getattr(chat, "id", None)
            if not self.is_authorized_chat(chat_id):
                self.logger.warning(
                    "Ignoring Telegram message from unauthorized chat",
                    extra={"chat_id": chat_id},
                )
                return
            try:
                message = await handler()
            except Exception as exc:  # pragma: no cover - ensure user is notified
                self.logger.exception("Telegram command failed", extra={"handler": handler.__name__})
                message = f"Command failed: {exc}"[:400]
            await self.send_message(message)

        return _wrapped

    async def _status_job(self, *_: Any) -> None:
        try:
            await self.send_status_update()
        except Exception:  # pragma: no cover - logged for observability
            self.logger.exception("Failed to push Telegram status update")

    async def start(self) -> None:
        if not self.config.can_run:
            self.logger.info("Telegram bot disabled or missing credentials; skipping startup")
            return
        if self._application is not None:
            return

        AIORateLimiter, ApplicationBuilder, CommandHandler, MessageHandler, filters = self._load_ptb()
        self.logger.info("Starting Telegram bot background tasks")
        application = (
            ApplicationBuilder()
            .token(self.config.token)
            .rate_limiter(AIORateLimiter())
            .build()
        )
        application.add_handler(CommandHandler("pause", self._command_wrapper(self._handle_pause)))
        application.add_handler(CommandHandler("resume", self._command_wrapper(self._handle_resume)))
        application.add_handler(CommandHandler("status", self._command_wrapper(self._handle_status)))
        application.add_handler(
            CommandHandler(
                ["close", "close_all", "closeall"],
                self._command_wrapper(self._handle_close_all),
            )
        )
        application.add_handler(MessageHandler(filters.COMMAND, self._command_wrapper(self._handle_unknown)))
        interval = max(1, int(self.config.push_minutes)) * 60
        if application.job_queue is not None:
            application.job_queue.run_repeating(
                self._status_job,
                interval=interval,
                first=10,
                name="opsbot-status",
            )

        await application.initialize()
        await application.start()
        updater = application.updater
        if updater is not None:
            self._polling_task = asyncio.create_task(
                updater.start_polling(drop_pending_updates=True),
                name="telegram-updater-polling",
            )
        self._application = application

    async def stop(self) -> None:
        if self._application is None:
            return
        updater = self._application.updater
        if updater is not None:
            await updater.stop()
        if self._polling_task is not None:
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None
        await self._application.stop()
        await self._application.shutdown()
        self._application = None
        self.logger.info("Telegram bot stopped")

    async def send_message(self, text: str) -> None:
        if not self.config.can_run:
            return
        if self._application is None:
            self.logger.warning("Telegram application not initialized; message skipped")
            return
        bot = getattr(self._application, "bot", None)
        if bot is None:
            self.logger.warning("Telegram bot API not ready; message skipped")
            return
        try:
            await bot.send_message(chat_id=self.config.chat_id, text=text)
        except Exception as exc:  # pragma: no cover - network failures are logged
            self.logger.warning("Failed to send Telegram message", extra={"error": str(exc)})

    async def send_status_update(self) -> None:
        if not self.config.can_run:
            return
        message = await build_status_message()
        await self.send_message(message)

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

    async def _handle_status(self) -> str:
        message = await build_status_message()
        return message

    async def _handle_close_all(self) -> str:
        state = get_state()
        environment = str(getattr(state.control, "environment", "")).lower()
        if environment != "testnet":
            return "Cancel-all only available on testnet profile."
        result = await cancel_all_orders()
        ledger.record_event(level="INFO", code="cancel_all", payload={"source": "telegram", **result})
        return "Cancel-all requested."

    async def _handle_unknown(self) -> str:
        return "Unknown command. Available: /pause, /resume, /status, /close"


def setup_telegram_bot(app: FastAPI) -> None:
    config = TelegramBotConfig.from_env()
    app.state.telegram_bot = None
    if not config.enabled:
        LOGGER.info("Telegram bot disabled; skipping setup")
        return

    bot = TelegramBot(config)
    app.state.telegram_bot = bot

    @app.on_event("startup")
    async def _start_bot() -> None:  # pragma: no cover - exercised in integration environments
        await bot.start()

    @app.on_event("shutdown")
    async def _stop_bot() -> None:  # pragma: no cover - exercised in integration environments
        await bot.stop()
