"""Feature-flagged chaos helpers for exchange adapters.

This module powers chaos and fault-injection utilities only; it intentionally
uses Python's :mod:`random` for probabilistic triggers that are unrelated to
security or cryptography.
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml


def _parse_float(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_int(value: str | None, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def _feature_enabled() -> bool:
    raw = os.getenv("FEATURE_CHAOS")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ChaosSettings:
    """Immutable snapshot of chaos injection parameters."""

    enabled: bool = False
    ws_drop_p: float = 0.0
    rest_timeout_p: float = 0.0
    order_delay_ms: int = 0
    profile: str = "none"

    def as_dict(self) -> dict[str, float | int | bool | str]:
        return {
            "enabled": self.enabled,
            "ws_drop_p": self.ws_drop_p,
            "rest_timeout_p": self.rest_timeout_p,
            "order_delay_ms": self.order_delay_ms,
            "profile": self.profile,
        }


_SETTINGS_LOCK = threading.Lock()
_SETTINGS: ChaosSettings | None = None
_PROFILES_PATH = Path(__file__).resolve().parents[2] / "configs" / "fault_profiles.yaml"


@lru_cache(maxsize=1)
def _load_profiles(path: Path | None = None) -> dict[str, dict[str, float | int]]:
    target = path or _PROFILES_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        payload = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}
    if isinstance(payload, Mapping) and "profiles" in payload:
        payload = payload.get("profiles")
    if not isinstance(payload, Mapping):
        return {}
    profiles: dict[str, dict[str, float | int]] = {}
    for name, raw_values in payload.items():
        if not isinstance(raw_values, Mapping):
            continue
        normalized: dict[str, float | int] = {}
        for key, value in raw_values.items():
            key_text = str(key or "").strip().lower()
            try:
                if key_text in {"ws_drop_p", "ws_drop_probability"}:
                    normalized["ws_drop_p"] = float(value)
                elif key_text in {"rest_timeout_p", "rest_timeout_probability"}:
                    normalized["rest_timeout_p"] = float(value)
                elif key_text in {"order_delay_ms", "order_delay"}:
                    normalized["order_delay_ms"] = int(float(value))
            except (TypeError, ValueError):
                continue
        profiles[str(name or "").strip().lower()] = normalized
    return profiles


def _profile_defaults(name: str | None) -> dict[str, float | int]:
    if not name:
        return {}
    profiles = _load_profiles()
    return profiles.get(name.strip().lower(), {})


def _extract_config_value(
    config: Mapping[str, Any] | Any, key: str, default: float | int = 0
) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default)


def resolve_settings(config: Mapping[str, Any] | Any | None = None) -> ChaosSettings:
    """Resolve settings from FEATURE_CHAOS, config payload, profiles and env overrides."""

    profile_name = os.getenv("CHAOS_PROFILE")
    profile_defaults = _profile_defaults(profile_name)

    if not _feature_enabled():
        return ChaosSettings(profile="none")

    ws_default = _parse_float(
        profile_defaults.get("ws_drop_p"),
        _parse_float(_extract_config_value(config, "ws_drop_probability", 0.0), 0.0),
    )
    rest_default = _parse_float(
        profile_defaults.get("rest_timeout_p"),
        _parse_float(_extract_config_value(config, "rest_timeout_probability", 0.0), 0.0),
    )
    order_delay_default = _parse_int(
        profile_defaults.get("order_delay_ms"),
        _parse_int(_extract_config_value(config, "order_delay_ms", 0), 0),
    )

    ws_drop_p = _parse_float(os.getenv("CHAOS_WS_DROP_P"), ws_default)
    rest_timeout_p = _parse_float(os.getenv("CHAOS_REST_TIMEOUT_P"), rest_default)
    order_delay_ms = _parse_int(os.getenv("CHAOS_ORDER_DELAY_MS"), order_delay_default)

    profile_label = str(profile_name or "custom").strip().lower() or "custom"

    return ChaosSettings(
        enabled=True,
        ws_drop_p=_clamp_probability(ws_drop_p),
        rest_timeout_p=_clamp_probability(rest_timeout_p),
        order_delay_ms=max(0, order_delay_ms),
        profile=profile_label,
    )


def configure(settings: ChaosSettings | None) -> None:
    """Persist the provided settings snapshot for subsequent lookups."""

    with _SETTINGS_LOCK:
        global _SETTINGS
        _SETTINGS = ChaosSettings() if settings is None else settings


def get_settings() -> ChaosSettings:
    """Return the current chaos settings snapshot."""

    with _SETTINGS_LOCK:
        global _SETTINGS
        if _SETTINGS is None:
            _SETTINGS = resolve_settings()
        return _SETTINGS


def should_drop_ws_update(settings: ChaosSettings | None = None) -> bool:
    payload = settings or get_settings()
    if not payload.enabled or payload.ws_drop_p <= 0.0:
        return False
    chaos_roll = random.random()  # nosec B311 - chaos probability trigger, not security sensitive
    return chaos_roll < payload.ws_drop_p


def maybe_raise_rest_timeout(
    settings: ChaosSettings | None = None, *, context: str | None = None
) -> None:
    payload = settings or get_settings()
    if not payload.enabled or payload.rest_timeout_p <= 0.0:
        return
    chaos_roll = random.random()  # nosec B311 - chaos probability trigger, not security sensitive
    if chaos_roll < payload.rest_timeout_p:
        if context:
            raise RuntimeError(f"chaos: simulated REST timeout ({context})")
        raise RuntimeError("chaos: simulated REST timeout")


def apply_order_delay(settings: ChaosSettings | None = None) -> None:
    payload = settings or get_settings()
    if not payload.enabled:
        return
    if payload.order_delay_ms <= 0:
        return
    time.sleep(payload.order_delay_ms / 1000.0)


__all__ = [
    "ChaosSettings",
    "apply_order_delay",
    "configure",
    "get_settings",
    "maybe_raise_rest_timeout",
    "resolve_settings",
    "should_drop_ws_update",
]
