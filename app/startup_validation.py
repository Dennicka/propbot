"""Fail-fast startup validation to guard unsafe deployments."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, Set

from .profile_config import (
    MissingSecretsError,
    ProfileConfig,
    ProfileConfigError,
    ensure_required_secrets,
    load_profile_config,
)
from .secrets_store import SecretsStore


LOGGER = logging.getLogger(__name__)

_PLACEHOLDER_TOKENS = ("change-me", "changeme", "todo", "replace-me", "fill-me")


def _load_template_env_names() -> Set[str]:
    root = Path(__file__).resolve().parent.parent
    template = root / ".env.prod.example"
    names: Set[str] = set()
    try:
        for line in template.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            name, _ = stripped.split("=", 1)
            name = name.strip()
            if not name:
                continue
            names.add(name)
    except OSError as exc:
        # Missing template is not fatal but reduces placeholder coverage.
        LOGGER.warning("startup_validation template read failed path=%s error=%s", template, exc)
    return names


_TEMPLATE_ENV_NAMES = _load_template_env_names()


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
        formatted = f"[FATAL CONFIG] {message}"
        LOGGER.error(formatted)
        errors.append(formatted)

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

    def require_path_defined(name: str, *, hint: str) -> None:
        if not _is_truthy(os.getenv(name)):
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
            except OSError as exc:
                fatal(
                    f"{description}: каталог {parent} отсутствует и не создаётся. "
                    "Примонтируй volume с записью или укажи другой путь."
                )
                LOGGER.error("startup_validation mkdir failed path=%s error=%s", parent, exc)
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
        except OSError as exc:
            fatal(
                f"{description}: {target} недоступен для записи. "
                "Выстави права на запись или укажи другой путь."
            )
            LOGGER.error("startup_validation write probe failed path=%s error=%s", target, exc)
            return
        finally:
            if not existed_before:
                try:
                    target.unlink()
                except OSError as exc:
                    LOGGER.warning(
                        "startup_validation cleanup failed path=%s error=%s", target, exc
                    )

    profile_cfg: ProfileConfig | None = None
    try:
        profile_cfg = load_profile_config()
    except ProfileConfigError as exc:
        fatal(str(exc))
    else:
        if profile_cfg.requires_secrets:
            secrets_path = os.getenv("SECRETS_STORE_PATH")
            if not secrets_path:
                fatal("PROFILE=live требует SECRETS_STORE_PATH с путём до JSON-хранилища ключей.")
            else:
                try:
                    store = SecretsStore(secrets_path)
                except Exception as exc:  # pragma: no cover - defensive
                    fatal(
                        "Не удалось загрузить secrets store: "
                        f"{exc} (путь: {secrets_path}). Проверь, что файл существует и доступен."
                    )
                else:
                    try:
                        ensure_required_secrets(
                            profile_cfg, lambda name: store.get_exchange_credentials(name)
                        )
                    except MissingSecretsError as exc:
                        fatal(str(exc))

    # ------------------------------------------------------------------
    # Placeholder detection to ensure template values were replaced.
    for name in sorted(_TEMPLATE_ENV_NAMES):
        raw_value = os.getenv(name)
        if raw_value is None:
            continue
        normalized = str(raw_value).strip().lower()
        if not normalized:
            continue
        if any(token in normalized for token in _PLACEHOLDER_TOKENS):
            fatal(
                f"{name} содержит плейсхолдер '{raw_value}'. "
                "Актуализируй .env.prod перед запуском."
            )

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
    require_path_defined(
        "RUNTIME_STATE_PATH",
        hint=(
            "RUNTIME_STATE_PATH пуст. Укажи путь для runtime_state_store, примонтированный к persistent volume."
        ),
    )
    require_path_defined(
        "POSITIONS_STORE_PATH",
        hint=("POSITIONS_STORE_PATH пуст. Пропиши файл для positions_store с сохранением на диск."),
    )
    require_path_defined(
        "PNL_HISTORY_PATH",
        hint=(
            "PNL_HISTORY_PATH пуст. Задай файл для pnl_history_store, чтобы снапшоты были persistent."
        ),
    )
    require_path_defined(
        "HEDGE_LOG_PATH",
        hint=(
            "HEDGE_LOG_PATH пуст. Укажи файл для журнала авто-хеджа на примонтированном storage."
        ),
    )
    require_path_defined(
        "OPS_ALERTS_FILE",
        hint=("OPS_ALERTS_FILE пуст. Укажи путь для журнала ops_alerts на persistent storage."),
    )
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
    ensure_path_writable(
        "PNL_HISTORY_PATH",
        default="data/pnl_history.json",
        description="PNL_HISTORY_PATH",
    )
    ensure_path_writable(
        "OPS_APPROVALS_FILE",
        default="data/ops_approvals.json",
        description="OPS_APPROVALS_FILE",
    )

    # ------------------------------------------------------------------
    # Live-trading safety levers.
    if not _env_flag("DRY_RUN_MODE") and not _env_flag("SAFE_MODE", True):
        fatal(
            "DRY_RUN_MODE=false запрещает старт без SAFE_MODE/HOLD. "
            "Запускай контейнер с SAFE_MODE=true и снимай HOLD только вручную через двухшаговый /resume-confirm."
        )

    return errors


def collect_startup_errors() -> list[str]:
    """Return collected startup validation errors without exiting."""

    return _collect_errors()


def validate_startup() -> None:
    """Validate environment and abort when unsafe configuration is detected."""

    errors = collect_startup_errors()
    if errors:
        raise SystemExit("\n".join(errors))


__all__ = ["collect_startup_errors", "validate_startup"]
