"""Operations bot integration for PropBot."""

from __future__ import annotations

from fastapi import FastAPI

from .telegram_bot import OpsTelegramBot, OpsTelegramBotConfig, set_ops_bot_instance
from .notify import set_ops_bot


def setup_opsbot(app: FastAPI) -> None:
    """Initialise the operations Telegram bot lifecycle hooks."""

    config = OpsTelegramBotConfig.from_env()
    bot = OpsTelegramBot(config)
    set_ops_bot_instance(bot)
    set_ops_bot(bot)
    app.state.ops_bot = bot

    @app.on_event("startup")
    async def _start_bot() -> None:  # pragma: no cover - integration hook
        await bot.start()

    @app.on_event("shutdown")
    async def _stop_bot() -> None:  # pragma: no cover - integration hook
        await bot.stop()
        set_ops_bot(None)


__all__ = ["setup_opsbot"]
