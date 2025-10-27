"""Fail-fast startup validation to guard unsafe deployments."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return bool(str(value).strip())


def _collect_errors() -> list[str]:
    errors: list[str] = []

    def fatal(message: str) -> None:
        errors.append(f"[FATAL CONFIG] {message}")

    def require_env(name: str, *, hint: str) -> None:
        if not _is_truthy(os.getenv(name)):
            fatal(hint)

    def require_any(names: Iterable[str], *, hint: str) -> None:
        if not any(_is_truthy(os.getenv(entry)) for entry in names):
            fatal(hint)

    def require_positive(name: str, *, hint: str) -> None:
        raw = os.getenv(name)
        if raw is None:
            return
        try:
            value = float(str(raw).strip())
        except ValueError:
            fatal(f"{hint} (получено '{raw}')")
            return
        if value <= 0:
            fatal(hint)

    def ensure_path_writable(name: str, default: str, *, description: str) -> None:
        raw = os.getenv(name)
        if raw:
            target = Path(raw).expanduser()
        else:
            target = Path(default)
        # ``Path('')`` is the current directory, make it explicit.
        if str(target) == "":
            target = Path(".")
        # For file-like paths we care about parent permissions.
        parent = target if target.is_dir() else target.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                fatal(
                    f"{description}: каталог {parent} отсутствует и не создаётся. "
                    "Примонтируй volume с записью или укажи другой путь."
                )
                return
        if not os.access(parent, os.W_OK | os.X_OK):
            fatal(
                f"{description}: нет прав на запись в каталог {parent}. "
                "Проверь монтирование и разрешения."
            )
            return

        existed_before = target.exists()
        try:
            with target.open("a", encoding="utf-8"):
                pass
        except OSError:
            fatal(
                f"{description}: {target} недоступен для записи. "
                "Выстави права на запись или укажи другой путь."
            )
            return
        finally:
            if not existed_before:
                try:
                    target.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Secrets and tokens guarded by feature flags.
    if _env_flag("TELEGRAM_ENABLE"):
        require_env(
            "TELEGRAM_BOT_TOKEN",
            hint="TELEGRAM_ENABLE=true но TELEGRAM_BOT_TOKEN не задан. Укажи токен или выключи TELEGRAM_ENABLE.",
        )
        require_env(
            "TELEGRAM_CHAT_ID",
            hint="TELEGRAM_ENABLE=true но TELEGRAM_CHAT_ID не задан. Укажи chat id или выключи TELEGRAM_ENABLE.",
        )

    if _env_flag("TELEGRAM_OPS_ENABLE"):
        require_any(
            ["TELEGRAM_OPS_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"],
            hint=(
                "TELEGRAM_OPS_ENABLE=true но токен Telegram (TELEGRAM_OPS_BOT_TOKEN или TELEGRAM_BOT_TOKEN) пустой. "
                "Укажи токен или выключи TELEGRAM_OPS_ENABLE."
            ),
        )
        require_any(
            ["TELEGRAM_OPS_CHAT_ID", "TELEGRAM_CHAT_ID"],
            hint=(
                "TELEGRAM_OPS_ENABLE=true но chat id (TELEGRAM_OPS_CHAT_ID или TELEGRAM_CHAT_ID) пустой. "
                "Пропиши chat id или выключи TELEGRAM_OPS_ENABLE."
            ),
        )

    auto_hedge_flag = _env_flag("AUTO_HEDGE_ENABLED") or _env_flag("AUTO_HEDGE_ENABLE")
    if auto_hedge_flag:
        has_binance_creds = any(
            all(_is_truthy(os.getenv(entry)) for entry in bundle)
            for bundle in (
                ("BINANCE_UM_API_KEY_TESTNET", "BINANCE_UM_API_SECRET_TESTNET"),
                ("BINANCE_LV_API_KEY", "BINANCE_LV_API_SECRET"),
                ("BINANCE_API_KEY", "BINANCE_API_SECRET"),
            )
        )
        has_okx_creds = any(
            all(_is_truthy(os.getenv(entry)) for entry in bundle)
            for bundle in (
                (
                    "OKX_API_KEY_TESTNET",
                    "OKX_API_SECRET_TESTNET",
                    "OKX_API_PASSPHRASE_TESTNET",
                ),
                ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"),
            )
        )
        if not has_binance_creds or not has_okx_creds:
            fatal(
                "AUTO_HEDGE_ENABLED=true требует валидные ключи Binance и OKX. "
                "Заполни пары BINANCE_* и OKX_* или выключи AUTO_HEDGE_ENABLED."
            )

    for limit_name in (
        "MAX_OPEN_POSITIONS",
        "MAX_NOTIONAL_PER_POSITION_USDT",
        "MAX_TOTAL_NOTIONAL_USDT",
        "MAX_LEVERAGE",
    ):
        require_positive(
            limit_name,
            hint=f"{limit_name} должен быть положительным. Проверь лимиты перед запуском.",
        )

    # ------------------------------------------------------------------
    # Global secrets required in production.
    require_env(
        "APPROVE_TOKEN",
        hint="APPROVE_TOKEN пуст. Укажи секрет второго оператора для /resume-confirm.",
    )

    if _env_flag("AUTH_ENABLED", True):
        require_env(
            "API_TOKEN",
            hint="AUTH_ENABLED=true но API_TOKEN не задан. Укажи bearer токен или выключи AUTH_ENABLED.",
        )

    # ------------------------------------------------------------------
    # File system locations.
    ensure_path_writable(
        "RUNTIME_STATE_PATH",
        default="data/runtime_state.json",
        description="RUNTIME_STATE_PATH",
    )
    ensure_path_writable(
        "POSITIONS_STORE_PATH",
        default="data/hedge_positions.json",
        description="POSITIONS_STORE_PATH",
    )
    ensure_path_writable(
        "HEDGE_LOG_PATH",
        default="data/hedge_log.json",
        description="HEDGE_LOG_PATH",
    )
    ensure_path_writable(
        "OPS_ALERTS_FILE",
        default="data/ops_alerts.json",
        description="OPS_ALERTS_FILE",
    )

    # ------------------------------------------------------------------
    # Live-trading safety levers.
    if not _env_flag("DRY_RUN_MODE") and not _env_flag("SAFE_MODE", True):
        fatal(
            "DRY_RUN_MODE=false запрещает старт без SAFE_MODE/HOLD. "
            "Запускай контейнер с SAFE_MODE=true и снимай HOLD только вручную через двухшаговый /resume-confirm."
        )

    return errors


def validate_startup() -> None:
    """Validate environment and abort when unsafe configuration is detected."""

    errors = _collect_errors()
    if errors:
        raise SystemExit("\n".join(errors))


__all__ = ["validate_startup"]
