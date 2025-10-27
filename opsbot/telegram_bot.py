"""Operations-focused Telegram bot with restricted controls."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import secrets
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict

from opsbot.notify import notify_ops

LOGGER = logging.getLogger(__name__)

_OPS_BOT_INSTANCE: "OpsTelegramBot | None" = None

_TELEGRAM_COMPONENTS: Dict[str, Any] | None = None
_TELEGRAM_IMPORT_FAILED = False


def _load_telegram_components() -> Dict[str, Any] | None:
    """Attempt to import python-telegram-bot primitives lazily."""

    global _TELEGRAM_COMPONENTS, _TELEGRAM_IMPORT_FAILED
    if _TELEGRAM_COMPONENTS is not None:
        return _TELEGRAM_COMPONENTS
    if _TELEGRAM_IMPORT_FAILED:
        return None
    try:
        telegram = importlib.import_module("telegram")
        telegram_ext = importlib.import_module("telegram.ext")
    except ModuleNotFoundError:
        _TELEGRAM_IMPORT_FAILED = True
        return None
    components = {
        "Application": getattr(telegram_ext, "Application"),
        "ApplicationBuilder": getattr(telegram_ext, "ApplicationBuilder"),
        "CommandHandler": getattr(telegram_ext, "CommandHandler"),
        "ContextTypes": getattr(telegram_ext, "ContextTypes", SimpleNamespace(DEFAULT_TYPE=Any)),
        "Update": getattr(telegram, "Update", SimpleNamespace),
    }
    _TELEGRAM_COMPONENTS = components
    return components


@dataclass(frozen=True)
class OpsTelegramBotConfig:
    token: str | None
    chat_id: str | None
    enabled: bool
    two_man_token: str | None

    @classmethod
    def from_env(cls) -> "OpsTelegramBotConfig":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        enabled = str(os.getenv("TELEGRAM_ENABLE", "false")).strip().lower() in {"1", "true", "yes", "on"}
        two_man_token = os.getenv("TWO_MAN_TOKEN")
        return cls(token=token, chat_id=chat_id, enabled=enabled, two_man_token=two_man_token)

    @property
    def can_run(self) -> bool:
        return self.enabled and bool(self.token) and bool(self.chat_id)


def set_ops_bot_instance(bot: "OpsTelegramBot | None") -> None:
    global _OPS_BOT_INSTANCE
    _OPS_BOT_INSTANCE = bot


def get_ops_bot_instance() -> "OpsTelegramBot | None":
    return _OPS_BOT_INSTANCE


async def build_status_report() -> str:
    """Assemble the status payload returned by /status."""

    from app import ledger
    from app.services import portfolio, risk
    from app.services.runtime import get_safety_status, get_state, set_open_orders
    from positions import list_open_positions

    snapshot = await portfolio.snapshot()
    open_orders = await asyncio.to_thread(ledger.fetch_open_orders)
    set_open_orders(open_orders)
    state = get_state()
    risk_state = risk.refresh_runtime_state(snapshot=snapshot, open_orders=open_orders)
    safety = get_safety_status()
    hold_active = bool(safety.get("hold_active"))
    hold_reason = safety.get("hold_reason")
    safe_mode = bool(getattr(state.control, "safe_mode", False))
    mode = str(getattr(state.control, "mode", "HOLD")).upper()
    breaches = getattr(risk_state, "breaches", []) or []
    if breaches:
        breach_summary = "; ".join(
            f"{getattr(breach, 'limit', '?')}:{getattr(breach, 'scope', '?')}"
            for breach in breaches
        )
    else:
        breach_summary = "OK"
    positions = list_open_positions()
    positions_summary = "none"
    if positions:
        formatted: list[str] = []
        for entry in positions:
            symbol = str(entry.get("symbol") or "").upper()
            long_venue = entry.get("long_venue") or entry.get("cheap_exchange")
            short_venue = entry.get("short_venue") or entry.get("expensive_exchange")
            notional = entry.get("notional_usdt")
            leverage = entry.get("leverage")
            summary = symbol or "?"
            venues: list[str] = []
            if long_venue or short_venue:
                venues.append(f"{long_venue or '?'} ↔ {short_venue or '?'}")
            stats: list[str] = []
            if notional is not None:
                try:
                    stats.append(f"notional={float(notional):.2f}")
                except (TypeError, ValueError):
                    stats.append(f"notional={notional}")
            if leverage is not None:
                try:
                    stats.append(f"leverage={float(leverage):.2f}x")
                except (TypeError, ValueError):
                    stats.append(f"leverage={leverage}")
            if venues:
                summary = f"{summary} {' '.join(venues)}"
            if stats:
                summary = f"{summary} ({', '.join(stats)})"
            formatted.append(summary)
        positions_summary = "\n".join(formatted)
    lines = [
        "SAFE_MODE={}".format(safe_mode),
        "MODE={}".format(mode),
        "HOLD_ACTIVE={}".format(hold_active),
        f"HOLD_REASON={hold_reason or 'n/a'}",
        f"RISK={breach_summary}",
        "OPEN_POSITIONS:",
        positions_summary,
    ]
    return "\n".join(lines)


class OpsTelegramBot:
    """Wrapper around python-telegram-bot with restricted commands."""

    def __init__(self, config: OpsTelegramBotConfig) -> None:
        self.config = config
        self._application: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._components: Dict[str, Any] | None = None
        self.logger = LOGGER

    async def start(self) -> None:
        if not self.config.can_run:
            self.logger.info("Ops Telegram bot disabled or missing credentials; skipping startup")
            return
        if self._application is not None:
            return
        components = _load_telegram_components()
        if components is None:
            self.logger.info("python-telegram-bot not installed; ops bot disabled")
            return
        builder: Callable[[], Any] = components["ApplicationBuilder"]
        command_handler = components["CommandHandler"]
        self._loop = asyncio.get_running_loop()
        self._application = builder().token(self.config.token).build()
        self._components = components
        self._register_handlers(self._application, command_handler)
        await self._application.initialize()
        await self._application.start()
        updater = getattr(self._application, "updater", None)
        if updater is not None:
            await updater.start_polling()
        self.logger.info("Ops Telegram bot started")

    async def stop(self) -> None:
        app = self._application
        if app is None:
            return
        updater = getattr(app, "updater", None)
        if updater is not None:
            await updater.stop()
        await app.stop()
        await app.shutdown()
        self._application = None
        self._loop = None
        self._components = None
        self.logger.info("Ops Telegram bot stopped")

    def publish_alert(self, text: str) -> None:
        if not text or not self.config.can_run or self._components is None:
            return
        if self._application is None or self._loop is None:
            return

        async def _send() -> None:
            try:
                await self._application.bot.send_message(chat_id=self.config.chat_id, text=text)
            except Exception:  # pragma: no cover - defensive logging
                self.logger.exception("Failed to push ops alert")

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except RuntimeError:  # pragma: no cover - occurs when loop closing
            self.logger.debug("Event loop unavailable for ops alert", exc_info=True)

    def _register_handlers(self, application: Any, command_handler_cls: Callable[..., Any]) -> None:
        application.add_handler(command_handler_cls("status", self._cmd_status))
        application.add_handler(command_handler_cls("positions", self._cmd_positions))
        application.add_handler(command_handler_cls("hold", self._cmd_hold))
        application.add_handler(command_handler_cls("resume_confirm", self._cmd_resume_confirm))
        application.add_handler(command_handler_cls("kill", self._cmd_kill))

    def _is_authorized(self, update: Any) -> bool:
        chat = update.effective_chat
        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            return False
        authorised = str(chat_id) == str(self.config.chat_id)
        if not authorised:
            self.logger.warning("Unauthorized Telegram command", extra={"chat_id": chat_id})
        return authorised

    async def _reply(self, update: Any, text: str) -> None:
        message = update.effective_message
        if message is None or not text:
            return
        try:
            await message.reply_text(text)
        except Exception:  # pragma: no cover - best effort response
            self.logger.exception("Failed to send Telegram reply")

    async def _cmd_status(self, update: Any, context: Any) -> None:
        if not self._is_authorized(update):
            return
        message = await self.process_status()
        await self._reply(update, message)

    async def _cmd_positions(self, update: Any, context: Any) -> None:
        if not self._is_authorized(update):
            return
        message = self.process_positions()
        await self._reply(update, message)

    async def _cmd_hold(self, update: Any, context: Any) -> None:
        if not self._is_authorized(update):
            return
        reason = " ".join(context.args) if context.args else "manual_hold"
        reason = reason.strip() or "manual_hold"
        message = await self.process_hold(reason=reason, requested_by="telegram")
        await self._reply(update, message)

    async def _cmd_resume_confirm(self, update: Any, context: Any) -> None:
        if not self._is_authorized(update):
            return
        if len(context.args) < 1:
            await self._reply(update, "Usage: /resume_confirm <token> [note]")
            return
        token = context.args[0]
        note = " ".join(context.args[1:]) if len(context.args) > 1 else ""
        message = await self.process_resume_confirm(token=token, note=note)
        await self._reply(update, message)

    async def _cmd_kill(self, update: Any, context: Any) -> None:
        if not self._is_authorized(update):
            return
        message = await self.process_kill()
        await self._reply(update, message)

    async def process_status(self) -> str:
        return await build_status_report()

    def process_positions(self) -> str:
        from positions import list_open_positions

        positions = list_open_positions()
        if not positions:
            return "No open hedge positions recorded."
        lines: list[str] = []
        for entry in positions:
            symbol = str(entry.get("symbol") or "").upper() or "?"
            long_venue = entry.get("long_venue") or entry.get("cheap_exchange") or "?"
            short_venue = entry.get("short_venue") or entry.get("expensive_exchange") or "?"
            notional = entry.get("notional_usdt")
            leverage = entry.get("leverage")
            spread = entry.get("entry_spread_bps")
            parts = [f"{symbol} {long_venue} ↔ {short_venue}"]
            metrics: list[str] = []
            if notional is not None:
                try:
                    metrics.append(f"notional={float(notional):.2f}")
                except (TypeError, ValueError):
                    metrics.append(f"notional={notional}")
            if leverage is not None:
                try:
                    metrics.append(f"leverage={float(leverage):.2f}x")
                except (TypeError, ValueError):
                    metrics.append(f"leverage={leverage}")
            if spread is not None:
                try:
                    metrics.append(f"spread={float(spread):.2f}bps")
                except (TypeError, ValueError):
                    metrics.append(f"spread={spread}")
            if metrics:
                parts.append(f"({', '.join(metrics)})")
            lines.append(" ".join(parts))
        return "\n".join(lines)

    async def process_hold(self, *, reason: str, requested_by: str | None = None) -> str:
        from app import ledger
        from app.services.loop import hold_loop
        from app.services.runtime import engage_safety_hold, set_mode

        await hold_loop()
        engage_safety_hold(reason, source="opsbot", metadata={"requested_by": requested_by or "opsbot"})
        set_mode("HOLD")
        ledger.record_event(
            level="INFO",
            code="mode_change",
            payload={"mode": "HOLD", "reason": reason, "requested_by": requested_by or "opsbot"},
        )
        return f"HOLD engaged ({reason})."

    async def process_resume_confirm(self, *, token: str, note: str) -> str:
        from app.services.runtime import approve_resume, get_safety_status, is_hold_active

        if not is_hold_active():
            return "Hold not active."
        safety = get_safety_status()
        resume_info = safety.get("resume_request") if isinstance(safety, dict) else None
        if not isinstance(resume_info, dict) or not resume_info.get("pending", True):
            return "No pending resume request."
        expected = self.config.two_man_token
        if not expected:
            return "TWO_MAN_TOKEN not configured."
        if not secrets.compare_digest(str(token), str(expected)):
            notify_ops(
                "RESUME CONFIRM DENIED",
                {
                    "reason": "invalid_token",
                    "source": "opsbot",
                    "note": note,
                },
            )
            return "Invalid approval token."
        result = approve_resume(actor="opsbot")
        details = {
            "reason": resume_info.get("reason"),
            "source": "opsbot",
            "note": note,
            "status": "hold_cleared" if result.get("hold_cleared") else "pending_hold",
        }
        notify_ops("RESUME CONFIRMED", details)
        return "Resume confirmed." if result.get("hold_cleared") else "Resume approved, hold still active."

    async def process_kill(self) -> str:
        from app import ledger
        from app.services import risk
        from app.services.loop import cancel_all_orders, hold_loop
        from app.services.runtime import HoldActiveError, get_safety_status, get_state, set_mode

        state = get_state()
        state.control.safe_mode = True
        set_mode("HOLD")
        await hold_loop()
        try:
            result = await cancel_all_orders()
        except HoldActiveError as exc:
            safety = get_safety_status()
            reason = safety.get("hold_reason") if isinstance(safety, dict) else str(exc)
            notify_ops("KILL FAILED", {"reason": reason, "source": "opsbot"})
            return f"Kill failed: hold active ({reason})."
        notify_ops(
            "KILL ACTIVATED",
            {"reason": "manual_kill", "source": "opsbot", "details": result},
        )
        risk.refresh_runtime_state()
        return "Kill switch engaged; all orders cancelled."


__all__ = [
    "OpsTelegramBot",
    "OpsTelegramBotConfig",
    "build_status_report",
    "get_ops_bot_instance",
    "set_ops_bot_instance",
]
