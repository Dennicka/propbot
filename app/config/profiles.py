"""Runtime profile helpers and launch-time safety checks."""

from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Iterable, Mapping, MutableMapping

from ..profile_config import (
    MissingSecretsError,
    ProfileConfig,
    ensure_required_secrets,
    load_profile_config,
)
from ..secrets_store import SecretsStore

LOGGER = logging.getLogger(__name__)


class ProfileSafetyError(RuntimeError):
    """Raised when a profile launch is considered unsafe."""

    def __init__(self, message: str, *, reasons: Iterable[str] | None = None) -> None:
        super().__init__(message)
        self.reasons = tuple(reasons or ())


class RuntimeProfile(str, enum.Enum):
    """Normalized runtime profile identifiers."""

    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"

    @classmethod
    def parse(cls, value: str | None) -> "RuntimeProfile":
        if not value:
            return cls.PAPER
        normalized = str(value).strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        raise ProfileSafetyError(f"Неизвестный профиль запуска: {value!r}")


def load_profile(profile: RuntimeProfile) -> ProfileConfig:
    """Load the :class:`ProfileConfig` for ``profile``."""

    return load_profile_config(profile.value)


def _bool_env(value: bool) -> str:
    return "true" if value else "false"


def _read_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    lowered = value.strip().lower()
    if not lowered:
        return default
    return lowered in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class _ProfileDefaults:
    safe_mode: bool
    dry_run_only: bool
    dry_run_mode: bool
    hedge_enabled: bool
    hedge_dry_run: bool
    feature_slo: bool
    health_guard_enabled: bool
    recon_enabled: bool
    watchdog_enabled: bool
    enable_place_test_orders: bool
    auto_hedge_enabled: bool
    wait_for_readiness: bool


_PROFILE_DEFAULTS: dict[RuntimeProfile, _ProfileDefaults] = {
    RuntimeProfile.PAPER: _ProfileDefaults(
        safe_mode=True,
        dry_run_only=True,
        dry_run_mode=True,
        hedge_enabled=False,
        hedge_dry_run=True,
        feature_slo=False,
        health_guard_enabled=False,
        recon_enabled=False,
        watchdog_enabled=False,
        enable_place_test_orders=False,
        auto_hedge_enabled=False,
        wait_for_readiness=False,
    ),
    RuntimeProfile.TESTNET: _ProfileDefaults(
        safe_mode=True,
        dry_run_only=False,
        dry_run_mode=True,
        hedge_enabled=True,
        hedge_dry_run=True,
        feature_slo=True,
        health_guard_enabled=True,
        recon_enabled=True,
        watchdog_enabled=False,
        enable_place_test_orders=True,
        auto_hedge_enabled=False,
        wait_for_readiness=True,
    ),
    RuntimeProfile.LIVE: _ProfileDefaults(
        safe_mode=True,
        dry_run_only=False,
        dry_run_mode=True,
        hedge_enabled=True,
        hedge_dry_run=False,
        feature_slo=True,
        health_guard_enabled=True,
        recon_enabled=True,
        watchdog_enabled=True,
        enable_place_test_orders=False,
        auto_hedge_enabled=False,
        wait_for_readiness=True,
    ),
}


_LIVE_CONFIRM_TOKEN = "I_KNOW_WHAT_I_AM_DOING"

_LIVE_LIMIT_ENV_REQUIREMENTS: tuple[tuple[str, str, bool], ...] = (
    (
        "MAX_TOTAL_NOTIONAL_USDT",
        "задай MAX_TOTAL_NOTIONAL_USDT (> 0) — лимит совокупного ноционала",
        True,
    ),
    (
        "DAILY_LOSS_CAP_USDT",
        "задай DAILY_LOSS_CAP_USDT (> 0) — дневной лимит убытков",
        True,
    ),
    (
        "MAX_OPEN_POSITIONS",
        "задай MAX_OPEN_POSITIONS (> 0) — лимит одновременно открытых сделок",
        True,
    ),
)


def apply_profile_environment(
    profile: RuntimeProfile,
    profile_cfg: ProfileConfig,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Apply profile defaults to ``environ`` and return applied values."""

    env = environ if environ is not None else os.environ
    defaults = _PROFILE_DEFAULTS[profile]
    applied: dict[str, str] = {}

    def _set(name: str, value: bool | str) -> None:
        string_value = value if isinstance(value, str) else _bool_env(value)
        env[name] = string_value
        applied[name] = string_value

    _set("PROFILE", profile.value)
    _set("DEFAULT_PROFILE", profile.value)
    _set("SAFE_MODE", defaults.safe_mode)
    _set("DRY_RUN_ONLY", profile_cfg.dry_run or defaults.dry_run_only)
    _set("DRY_RUN_MODE", defaults.dry_run_mode)
    _set("HEDGE_ENABLED", defaults.hedge_enabled)
    _set("HEDGE_DRY_RUN", defaults.hedge_dry_run)
    _set("FEATURE_SLO", defaults.feature_slo)
    _set("HEALTH_GUARD_ENABLED", defaults.health_guard_enabled or profile_cfg.flags.health_guard)
    _set("RECON_ENABLED", defaults.recon_enabled or profile_cfg.flags.recon)
    _set("WATCHDOG_ENABLED", defaults.watchdog_enabled or profile_cfg.flags.watchdog)
    _set("ENABLE_PLACE_TEST_ORDERS", defaults.enable_place_test_orders)
    _set("AUTO_HEDGE_ENABLED", defaults.auto_hedge_enabled)
    _set("WAIT_FOR_LIVE_READINESS_ON_START", defaults.wait_for_readiness)

    return applied


def resolve_guard_status(
    profile_cfg: ProfileConfig | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    """Return a snapshot of guard toggles inferred from the environment."""

    env = environ if environ is not None else os.environ
    flags = getattr(profile_cfg, "flags", None)
    slo_default = bool(getattr(flags, "slo", False))
    health_default = bool(getattr(flags, "health_guard", False))
    recon_default = bool(getattr(flags, "recon", False))
    watchdog_default = bool(getattr(flags, "watchdog", False))

    slo_enabled = _read_bool(env.get("FEATURE_SLO"), slo_default)
    hedge_enabled = _read_bool(env.get("HEDGE_ENABLED"), False)
    auto_hedge_enabled = _read_bool(env.get("AUTO_HEDGE_ENABLED"), False)
    health_enabled = _read_bool(env.get("HEALTH_GUARD_ENABLED"), health_default)
    recon_enabled = _read_bool(env.get("RECON_ENABLED"), recon_default)
    watchdog_enabled = _read_bool(env.get("WATCHDOG_ENABLED"), watchdog_default)

    return {
        "slo": slo_enabled,
        "hedge": hedge_enabled or auto_hedge_enabled,
        "health": health_enabled,
        "recon": recon_enabled,
        "watchdog": watchdog_enabled,
        "partial_hedge": hedge_enabled,
        "auto_hedge": auto_hedge_enabled,
    }


def ensure_live_prerequisites(
    profile_cfg: ProfileConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Ensure the live profile has secrets and risk guards enabled."""

    env = environ if environ is not None else os.environ
    errors: list[str] = []

    if not profile_cfg.requires_secrets:
        errors.append("PROFILE=live должен требовать secrets (requires_secrets=true)")

    try:
        store = SecretsStore()
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(
            "SECRETS_STORE_PATH не задан или secrets store недоступен: "
            f"{exc}"
        )
    else:
        try:
            ensure_required_secrets(profile_cfg, store.get_exchange_credentials)
        except MissingSecretsError as exc:
            errors.append(str(exc))

    guard_status = resolve_guard_status(profile_cfg, environ=env)
    if not guard_status["slo"]:
        errors.append("FEATURE_SLO=false выключает SLO guard — включи перед запуском live")
    if not guard_status["health"]:
        errors.append(
            "HEALTH_GUARD_ENABLED=false или guard_disabled в конфиге — включи account health guard"
        )
    if not guard_status["watchdog"]:
        errors.append("WATCHDOG_ENABLED=false выключает exchange watchdog — включи для live")
    if not guard_status["recon"]:
        errors.append("RECON_ENABLED=false выключает reconciliation runner — включи для live")
    if not guard_status["hedge"]:
        errors.append(
            "Неактивен ни один hedge-guard (HEDGE_ENABLED/AUTO_HEDGE_ENABLED). Включи частичный или авто-хедж"
        )

    if errors:
        details = "\n".join(f"- {entry}" for entry in errors)
        raise ProfileSafetyError(
            "Запуск профиля live заблокирован:\n" f"{details}", reasons=errors
        )


def ensure_live_acknowledged(
    profile_cfg: ProfileConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Ensure the operator explicitly acknowledged live launch risk limits."""

    _ = profile_cfg  # currently unused but kept for future linkage to limits
    env = environ if environ is not None else os.environ
    errors: list[str] = []

    token = (env.get("LIVE_CONFIRM") or "").strip()
    if token != _LIVE_CONFIRM_TOKEN:
        errors.append(
            "LIVE_CONFIRM должен быть установлен в 'I_KNOW_WHAT_I_AM_DOING' перед запуском live"
        )

    for name, description, require_positive in _LIVE_LIMIT_ENV_REQUIREMENTS:
        raw = env.get(name)
        if raw is None or not str(raw).strip():
            errors.append(f"{description}: переменная {name} не задана")
            continue
        try:
            numeric = float(str(raw))
        except ValueError:
            errors.append(
                f"{description}: переменная {name} должна быть числом (получено {raw!r})"
            )
            continue
        if require_positive and numeric <= 0:
            errors.append(f"{description}: значение {name} должно быть > 0 (получено {numeric})")

    if errors:
        details = "\n".join(f"- {entry}" for entry in errors)
        raise ProfileSafetyError(
            "LIVE запуск заблокирован политикой risk-confirmation:\n" f"{details}",
            reasons=errors,
        )


def ensure_profile_safe(
    profile: RuntimeProfile,
    profile_cfg: ProfileConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Validate that ``profile`` can be launched safely."""

    if profile is RuntimeProfile.LIVE:
        ensure_live_prerequisites(profile_cfg, environ=environ)
        ensure_live_acknowledged(profile_cfg, environ=environ)


__all__ = [
    "ProfileSafetyError",
    "RuntimeProfile",
    "apply_profile_environment",
    "ensure_live_acknowledged",
    "ensure_live_prerequisites",
    "ensure_profile_safe",
    "load_profile",
    "resolve_guard_status",
]

