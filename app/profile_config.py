"""Runtime profile loader used to configure launch presets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .config.loader import load_yaml


class ProfileConfigError(RuntimeError):
    """Raised when a profile configuration file is invalid."""


class MissingSecretsError(ProfileConfigError):
    """Raised when the profile requires secrets that are absent."""


def _ensure_mapping(payload: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise ProfileConfigError(f"{context} должен быть словарём, получено {type(payload)!r}")
    return payload


def _as_bool(value: object, *, context: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ProfileConfigError(f"{context} должен быть bool, получено {value!r}")


def _as_float(value: object, *, context: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ProfileConfigError(f"{context} должен быть числом, получено {value!r}") from None


def _as_str(value: object, *, context: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ProfileConfigError(f"{context} должен быть непустой строкой, получено {value!r}")


@dataclass(frozen=True)
class EndpointSettings:
    rest: str
    websocket: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, context: str) -> "EndpointSettings":
        rest = _as_str(payload.get("rest"), context=f"{context}.rest")
        websocket = _as_str(payload.get("websocket"), context=f"{context}.websocket")
        return cls(rest=rest, websocket=websocket)


@dataclass(frozen=True)
class BrokerSettings:
    venue: str
    endpoints: EndpointSettings

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, context: str) -> "BrokerSettings":
        venue = _as_str(payload.get("venue"), context=f"{context}.venue")
        endpoints_payload = _ensure_mapping(
            payload.get("endpoints"), context=f"{context}.endpoints"
        )
        endpoints = EndpointSettings.from_mapping(endpoints_payload, context=f"{context}.endpoints")
        return cls(venue=venue, endpoints=endpoints)


@dataclass(frozen=True)
class BrokerConfig:
    primary: BrokerSettings
    hedge: BrokerSettings | None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, context: str) -> "BrokerConfig":
        primary_payload = _ensure_mapping(payload.get("primary"), context=f"{context}.primary")
        primary = BrokerSettings.from_mapping(primary_payload, context=f"{context}.primary")
        hedge_data = payload.get("hedge")
        hedge: BrokerSettings | None = None
        if hedge_data is not None:
            hedge_payload = _ensure_mapping(hedge_data, context=f"{context}.hedge")
            hedge = BrokerSettings.from_mapping(hedge_payload, context=f"{context}.hedge")
        return cls(primary=primary, hedge=hedge)


@dataclass(frozen=True)
class RiskLimits:
    max_total_notional_usd: float
    max_single_position_usd: float
    max_drawdown_bps: float
    daily_loss_cap_usd: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, context: str) -> "RiskLimits":
        return cls(
            max_total_notional_usd=_as_float(
                payload.get("max_total_notional_usd"), context=f"{context}.max_total_notional_usd"
            ),
            max_single_position_usd=_as_float(
                payload.get("max_single_position_usd"), context=f"{context}.max_single_position_usd"
            ),
            max_drawdown_bps=_as_float(
                payload.get("max_drawdown_bps"), context=f"{context}.max_drawdown_bps"
            ),
            daily_loss_cap_usd=_as_float(
                payload.get("daily_loss_cap_usd"), context=f"{context}.daily_loss_cap_usd"
            ),
        )


@dataclass(frozen=True)
class ProfileFlags:
    health_guard: bool
    recon: bool
    slo: bool
    watchdog: bool

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, context: str) -> "ProfileFlags":
        return cls(
            health_guard=_as_bool(payload.get("health_guard"), context=f"{context}.health_guard"),
            recon=_as_bool(payload.get("recon"), context=f"{context}.recon"),
            slo=_as_bool(payload.get("slo"), context=f"{context}.slo"),
            watchdog=_as_bool(payload.get("watchdog"), context=f"{context}.watchdog"),
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "health_guard": self.health_guard,
            "recon": self.recon,
            "slo": self.slo,
            "watchdog": self.watchdog,
        }


@dataclass(frozen=True)
class ProfileSecret:
    name: str
    required: bool
    fields: tuple[str, ...]
    placeholders: Mapping[str, str]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, context: str) -> "ProfileSecret":
        name = _as_str(payload.get("name"), context=f"{context}.name")
        required = _as_bool(payload.get("required", False), context=f"{context}.required")
        fields_payload = _ensure_mapping(payload.get("fields"), context=f"{context}.fields")
        fields: list[str] = []
        placeholders: dict[str, str] = {}
        for key, value in fields_payload.items():
            field_name = _as_str(key, context=f"{context}.fields")
            fields.append(field_name)
            placeholders[field_name] = _as_str(value, context=f"{context}.fields.{field_name}")
        return cls(name=name, required=required, fields=tuple(fields), placeholders=placeholders)


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    description: str
    dry_run: bool
    requires_secrets: bool
    broker: BrokerConfig
    risk_limits: RiskLimits
    flags: ProfileFlags
    secrets: tuple[ProfileSecret, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ProfileConfig":
        profile_name = _as_str(payload.get("profile"), context="profile")
        description = _as_str(payload.get("description"), context="description")
        dry_run = _as_bool(payload.get("dry_run"), context="dry_run")
        requires_secrets = _as_bool(payload.get("requires_secrets", False), context="requires_secrets")
        broker_payload = _ensure_mapping(payload.get("broker"), context="broker")
        broker = BrokerConfig.from_mapping(broker_payload, context="broker")
        risk_payload = _ensure_mapping(payload.get("risk_limits"), context="risk_limits")
        risk_limits = RiskLimits.from_mapping(risk_payload, context="risk_limits")
        flags_payload = _ensure_mapping(payload.get("flags"), context="flags")
        flags = ProfileFlags.from_mapping(flags_payload, context="flags")
        secrets_payload = payload.get("secrets") or []
        if not isinstance(secrets_payload, Iterable):
            raise ProfileConfigError("secrets должен быть списком")
        secrets = []
        for index, entry in enumerate(secrets_payload):
            entry_mapping = _ensure_mapping(entry, context=f"secrets[{index}]")
            secrets.append(
                ProfileSecret.from_mapping(entry_mapping, context=f"secrets[{index}]")
            )
        return cls(
            name=profile_name,
            description=description,
            dry_run=dry_run,
            requires_secrets=requires_secrets,
            broker=broker,
            risk_limits=risk_limits,
            flags=flags,
            secrets=tuple(secrets),
        )

    def required_secret_names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.secrets if entry.required)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_profile_config(profile: str | None = None, *, path: Path | None = None) -> ProfileConfig:
    """Load ``ProfileConfig`` for ``profile`` or the active ``PROFILE`` env value."""

    if path is None:
        name = profile or os.environ.get("PROFILE") or "paper"
        slug = _as_str(name, context="profile").lower()
        candidate = _repository_root() / "configs" / f"profile.{slug}.yaml"
        path = candidate
    if not Path(path).is_file():
        raise ProfileConfigError(f"Не найден профиль конфигурации: {path}")
    payload = load_yaml(path)
    return ProfileConfig.from_mapping(payload)


def ensure_required_secrets(
    profile: ProfileConfig,
    secret_provider: Callable[[str], Mapping[str, object] | None],
) -> None:
    """Ensure required secrets for ``profile`` are available via ``secret_provider``."""

    if not profile.requires_secrets:
        return

    missing: list[str] = []
    for entry in profile.secrets:
        if not entry.required:
            continue
        provided = secret_provider(entry.name) or {}
        missing_fields = [field for field in entry.fields if not provided.get(field)]
        if missing_fields:
            missing.append(f"{entry.name}: {', '.join(missing_fields)}")
    if missing:
        details = ", ".join(missing)
        raise MissingSecretsError(
            f"PROFILE={profile.name} требует валидные секреты ({details}). "
            "Заполни secrets store перед запуском."
        )


__all__ = [
    "BrokerConfig",
    "MissingSecretsError",
    "ProfileConfig",
    "ProfileConfigError",
    "RiskLimits",
    "ensure_required_secrets",
    "load_profile_config",
]
