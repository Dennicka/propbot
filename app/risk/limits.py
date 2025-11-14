"""In-memory risk limits enforcement."""

from __future__ import annotations

import logging
import os
import time
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, Mapping, Optional, Tuple, TypedDict

from app.alerts.levels import AlertLevel
from app.alerts.pipeline import OpsAlertsPipeline, RISK_LIMIT_BREACHED
from app.config import feature_flags
from app.config.profile import is_canary_mode_enabled


LOGGER = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
_DEFAULT_SAFE_MODE_NOTIONAL = Decimal("0.1")
_DEFAULT_SAFE_MODE_DAILY = Decimal("0.25")


def _current_profile() -> str | None:
    raw = os.getenv("EXEC_PROFILE")
    if raw is None:
        return None
    value = raw.strip()
    return value or None


@dataclass
class RiskConfig:
    cap_per_venue: Dict[str, Decimal] = field(default_factory=dict)
    cap_per_symbol: Dict[Tuple[str, str], Decimal] = field(default_factory=dict)
    cap_per_strategy: Dict[str, Decimal] = field(default_factory=dict)
    daily_loss_limit: Optional[Decimal] = None
    daily_cooloff_sec: int = 3600
    max_consecutive_rejects: int = 3
    rejects_cooloff_sec: int = 300


@dataclass
class RiskState:
    day_ymd: str = ""
    realized_pnl: Decimal = Decimal("0")
    in_cooloff_until: int = 0
    rejects: Dict[Tuple[str, str, str], int] = field(default_factory=dict)
    key_cooloff_until: Dict[Tuple[str, str, str], int] = field(default_factory=dict)


class RiskLimitsSnapshot(TypedDict):
    enabled: bool
    max_notional_per_venue: dict[str, float]
    max_notional_per_symbol: dict[str, float]
    daily_loss_limit: float | None
    daily_loss_used: float | None
    rejects_recent: int | None
    extra: dict[str, float]


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _safe_mode_global_flag(default: bool = False) -> bool:
    return _env_flag("SAFE_MODE_GLOBAL", default)


def _parse_decimal_env(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name)
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        LOGGER.warning("risk.safe_mode_scale_invalid", extra={"name": name, "value": raw})
        return default
    return value


def _clamp_multiplier(value: Decimal) -> Decimal:
    if value <= Decimal("0"):
        return Decimal("0")
    if value >= Decimal("1"):
        return Decimal("1")
    return value


def _safe_mode_notional_multiplier() -> Decimal:
    return _clamp_multiplier(
        _parse_decimal_env("SAFE_MODE_SCALE_NOTIONAL", _DEFAULT_SAFE_MODE_NOTIONAL)
    )


def _safe_mode_daily_loss_multiplier() -> Decimal:
    return _clamp_multiplier(
        _parse_decimal_env("SAFE_MODE_SCALE_DAILY_LOSS", _DEFAULT_SAFE_MODE_DAILY)
    )


def _scale_decimal(value: Decimal, multiplier: Decimal) -> Decimal:
    if value <= Decimal("0"):
        return value
    scaled = value * multiplier
    if scaled > value:
        return value
    return scaled


def _scale_decimal_map(mapping: Dict[str, Decimal], multiplier: Decimal) -> Dict[str, Decimal]:
    if not mapping:
        return {}
    return {key: _scale_decimal(amount, multiplier) for key, amount in mapping.items()}


def _scale_symbol_map(
    mapping: Dict[Tuple[str, str], Decimal], multiplier: Decimal
) -> Dict[Tuple[str, str], Decimal]:
    if not mapping:
        return {}
    return {key: _scale_decimal(amount, multiplier) for key, amount in mapping.items()}


def _scale_config_for_safe_mode(
    cfg: "RiskConfig", multiplier_notional: Decimal, multiplier_daily: Decimal
) -> "RiskConfig":
    scaled = RiskConfig(
        cap_per_venue=_scale_decimal_map(cfg.cap_per_venue, multiplier_notional),
        cap_per_symbol=_scale_symbol_map(cfg.cap_per_symbol, multiplier_notional),
        cap_per_strategy=dict(cfg.cap_per_strategy),
        daily_loss_limit=(
            _scale_decimal(cfg.daily_loss_limit, multiplier_daily)
            if cfg.daily_loss_limit is not None
            else None
        ),
        daily_cooloff_sec=cfg.daily_cooloff_sec,
        max_consecutive_rejects=cfg.max_consecutive_rejects,
        rejects_cooloff_sec=cfg.rejects_cooloff_sec,
    )
    return scaled


class RiskGovernor:
    def __init__(self, cfg: RiskConfig, *, alerts: OpsAlertsPipeline | None = None):
        self.cfg = cfg
        self.state = RiskState()
        self._alerts = alerts

    def _emit_alert(
        self,
        *,
        limit_type: str,
        reason: str,
        venue: str,
        symbol: str,
        strategy: str,
        context: Mapping[str, object] | None = None,
    ) -> None:
        if self._alerts is None:
            return
        payload: Dict[str, object] = {
            "limit_type": limit_type,
            "reason": reason,
            "venue": str(venue or ""),
            "symbol": str(symbol or ""),
            "strategy": str(strategy or ""),
        }
        if context:
            payload.update(dict(context))
        profile = _current_profile()
        if profile:
            payload["profile"] = profile
        self._alerts.notify_event(
            event_type=RISK_LIMIT_BREACHED,
            message=f"Risk limit breached: {limit_type}",
            level=AlertLevel.CRITICAL,
            context=payload,
        )

    def _today(self, now_s: int) -> str:
        return time.strftime("%Y%m%d", time.gmtime(now_s))

    def _reset_day_if_needed(self, now_s: int) -> None:
        today = self._today(now_s)
        if self.state.day_ymd == today:
            return
        self.state.day_ymd = today
        self.state.realized_pnl = Decimal("0")
        self.state.in_cooloff_until = 0
        self.state.rejects.clear()
        self.state.key_cooloff_until.clear()

    def _check_global_cooloff(self, now_s: int) -> Optional[str]:
        expiry = self.state.in_cooloff_until
        if expiry <= 0:
            return None
        if now_s >= expiry:
            self.state.in_cooloff_until = 0
            return None
        return "daily_cooloff"

    def _check_key_cooloff(self, key: Tuple[str, str, str], now_s: int) -> Optional[str]:
        expiry = self.state.key_cooloff_until.get(key)
        if expiry is None:
            return None
        if now_s >= expiry:
            self.state.key_cooloff_until.pop(key, None)
            self.state.rejects.pop(key, None)
            return None
        return "key_cooloff"

    def _check_notional_caps(
        self,
        venue: str,
        symbol: str,
        strategy: str,
        notional: Decimal,
    ) -> Tuple[Optional[str], Dict[str, object]]:
        venue_cap = self.cfg.cap_per_venue.get(venue)
        if venue_cap is not None and notional > venue_cap:
            return "venue_cap", {"limit": venue_cap, "scope": venue}
        symbol_cap = self.cfg.cap_per_symbol.get((venue, symbol))
        if symbol_cap is not None and notional > symbol_cap:
            return "symbol_cap", {"limit": symbol_cap, "scope": f"{venue}:{symbol}"}
        strategy_cap = self.cfg.cap_per_strategy.get(strategy)
        if strategy_cap is not None and notional > strategy_cap:
            return "strategy_cap", {"limit": strategy_cap, "scope": strategy}
        return None, {}

    def allow_order(
        self,
        venue: str,
        symbol: str,
        strategy: str,
        price: Decimal,
        qty: Decimal,
        now_s: Optional[int] = None,
    ) -> Tuple[bool, str]:
        now_value = int(now_s if now_s is not None else time.time())
        self._reset_day_if_needed(now_value)
        reason = self._check_global_cooloff(now_value)
        if reason is not None:
            self._emit_alert(
                limit_type="daily_cooloff",
                reason=reason,
                venue=venue,
                symbol=symbol,
                strategy=strategy,
                context={"cooloff_until": self.state.in_cooloff_until},
            )
            return False, reason
        key = (venue, symbol, strategy)
        reason = self._check_key_cooloff(key, now_value)
        if reason is not None:
            self._emit_alert(
                limit_type="key_cooloff",
                reason=reason,
                venue=venue,
                symbol=symbol,
                strategy=strategy,
                context={
                    "cooloff_until": self.state.key_cooloff_until.get(key, 0),
                    "rejects": self.state.rejects.get(key, 0),
                },
            )
            return False, reason
        qty_abs = qty.copy_abs()
        price_abs = price.copy_abs()
        notional = price_abs * qty_abs
        reason, details = self._check_notional_caps(venue, symbol, strategy, notional)
        if reason is not None:
            context: Dict[str, object] = {"notional": notional}
            context.update(details)
            self._emit_alert(
                limit_type=reason,
                reason=reason,
                venue=venue,
                symbol=symbol,
                strategy=strategy,
                context=context,
            )
            return False, reason
        return True, ""

    def on_reject(
        self,
        venue: str,
        symbol: str,
        strategy: str,
        now_s: Optional[int] = None,
    ) -> None:
        now_value = int(now_s if now_s is not None else time.time())
        self._reset_day_if_needed(now_value)
        if self.cfg.max_consecutive_rejects <= 0:
            return
        key = (venue, symbol, strategy)
        count = self.state.rejects.get(key, 0) + 1
        self.state.rejects[key] = count
        if count >= self.cfg.max_consecutive_rejects:
            until = now_value + max(self.cfg.rejects_cooloff_sec, 0)
            self.state.key_cooloff_until[key] = until
            self.state.rejects[key] = 0
            LOGGER.warning(
                "risk.governor_reject_cooloff",
                extra={
                    "event": "risk_governor_reject_cooloff",
                    "component": "risk_limits",
                    "details": {
                        "venue": venue,
                        "symbol": symbol,
                        "strategy": strategy,
                        "cooloff_until": until,
                    },
                },
            )
            self._emit_alert(
                limit_type="reject_cooloff",
                reason="reject_cooloff",
                venue=venue,
                symbol=symbol,
                strategy=strategy,
                context={"cooloff_until": until, "reject_count": count},
            )

    def on_ack(self, venue: str, symbol: str, strategy: str) -> None:
        key = (venue, symbol, strategy)
        self.state.rejects.pop(key, None)
        self.state.key_cooloff_until.pop(key, None)

    def on_filled(
        self,
        venue: str,
        symbol: str,
        strategy: str,
        realized_pnl: Decimal,
        now_s: Optional[int] = None,
    ) -> None:
        now_value = int(now_s if now_s is not None else time.time())
        self._reset_day_if_needed(now_value)
        self.state.realized_pnl += realized_pnl
        limit = self.cfg.daily_loss_limit
        if limit is None or limit <= Decimal("0"):
            return
        if self.state.realized_pnl <= -limit:
            until = now_value + max(self.cfg.daily_cooloff_sec, 0)
            if until > self.state.in_cooloff_until:
                self.state.in_cooloff_until = until
            LOGGER.warning(
                "risk.governor_daily_cooloff",
                extra={
                    "event": "risk_governor_daily_cooloff",
                    "component": "risk_limits",
                    "details": {
                        "venue": venue,
                        "symbol": symbol,
                        "strategy": strategy,
                        "realized_pnl": str(self.state.realized_pnl),
                        "cooloff_until": self.state.in_cooloff_until,
                    },
                },
            )
            self._emit_alert(
                limit_type="daily_loss_limit",
                reason="daily_loss_limit",
                venue=venue,
                symbol=symbol,
                strategy=strategy,
                context={
                    "cooloff_until": self.state.in_cooloff_until,
                    "realized_pnl": self.state.realized_pnl,
                    "limit": limit,
                    "scope": "global",
                },
            )


def load_config_from_env(
    *,
    apply_safe_mode: bool = True,
    is_canary: bool | None = None,
    safe_mode_global: bool | None = None,
) -> RiskConfig:
    def _parse_map(s: str, sep_items: str = ",", sep_kv: str = ":") -> Dict[str, Decimal]:
        out: Dict[str, Decimal] = {}
        if not s:
            return out
        for item in filter(None, (x.strip() for x in s.split(sep_items))):
            k, v = item.split(sep_kv, 1)
            out[k.strip()] = Decimal(v.strip())
        return out

    def _parse_map3(s: str) -> Dict[Tuple[str, str], Decimal]:
        out: Dict[Tuple[str, str], Decimal] = {}
        if not s:
            return out
        for item in filter(None, (x.strip() for x in s.split(";"))):
            a, b, v = item.split(":", 2)
            out[(a.strip(), b.strip())] = Decimal(v.strip())
        return out

    cfg = RiskConfig()
    cfg.cap_per_venue = _parse_map(os.getenv("RISK_CAP_VENUE", ""))
    cfg.cap_per_symbol = _parse_map3(os.getenv("RISK_CAP_SYMBOL", ""))
    cfg.cap_per_strategy = _parse_map(os.getenv("RISK_CAP_STRATEGY", ""))
    dloss = os.getenv("RISK_DAILY_LOSS", "").strip()
    if dloss:
        cfg.daily_loss_limit = Decimal(dloss)
    cfg.daily_cooloff_sec = int(os.getenv("RISK_DAILY_COOLOFF_SEC", "3600"))
    cfg.max_consecutive_rejects = int(os.getenv("RISK_MAX_CONSEC_REJECTS", "3"))
    cfg.rejects_cooloff_sec = int(os.getenv("RISK_REJECTS_COOLOFF_SEC", "300"))

    if apply_safe_mode:
        canary_flag = is_canary if is_canary is not None else is_canary_mode_enabled()
        safe_mode_flag = (
            safe_mode_global if safe_mode_global is not None else _safe_mode_global_flag()
        )
        if canary_flag or safe_mode_flag:
            cfg = _scale_config_for_safe_mode(
                cfg,
                multiplier_notional=_safe_mode_notional_multiplier(),
                multiplier_daily=_safe_mode_daily_loss_multiplier(),
            )
    return cfg


def _as_float(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _serialise_cap_map(mapping: Dict[str, Decimal]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, value in mapping.items():
        numeric = _as_float(value)
        if numeric is None:
            continue
        result[str(key)] = numeric
    return result


def _serialise_symbol_map(mapping: Dict[Tuple[str, str], Decimal]) -> dict[str, float]:
    result: dict[str, float] = {}
    for (venue, symbol), value in mapping.items():
        numeric = _as_float(value)
        if numeric is None:
            continue
        result[f"{venue}:{symbol}"] = numeric
    return result


def apply_safe_mode_scaling(
    raw_limits: RiskLimitsSnapshot, *, is_canary: bool, safe_mode_global: bool
) -> RiskLimitsSnapshot:
    """Apply safe-mode scaling to a risk limits snapshot when required."""

    if not (is_canary or safe_mode_global):
        return raw_limits

    scaled = deepcopy(raw_limits)
    notional_factor = float(_safe_mode_notional_multiplier())
    daily_factor = float(_safe_mode_daily_loss_multiplier())

    notional_factor = min(max(notional_factor, 0.0), 1.0)
    daily_factor = min(max(daily_factor, 0.0), 1.0)

    venue_caps = raw_limits.get("max_notional_per_venue", {})
    scaled["max_notional_per_venue"] = {
        key: max(value * notional_factor, 0.0) for key, value in venue_caps.items()
    }

    symbol_caps = raw_limits.get("max_notional_per_symbol", {})
    scaled["max_notional_per_symbol"] = {
        key: max(value * notional_factor, 0.0) for key, value in symbol_caps.items()
    }

    daily_limit = raw_limits.get("daily_loss_limit")
    if isinstance(daily_limit, (int, float)):
        scaled["daily_loss_limit"] = max(daily_limit * daily_factor, 0.0)

    return scaled


def get_risk_limits_snapshot() -> RiskLimitsSnapshot:
    """Return configured risk limits for UI consumers."""

    enabled = bool(feature_flags.risk_limits_on())
    snapshot: RiskLimitsSnapshot = {
        "enabled": enabled,
        "max_notional_per_venue": {},
        "max_notional_per_symbol": {},
        "daily_loss_limit": None,
        "daily_loss_used": None,
        "rejects_recent": None,
        "extra": {},
    }
    if not enabled:
        return snapshot
    try:
        canary_flag = is_canary_mode_enabled()
        safe_mode_flag = _safe_mode_global_flag()
        cfg = load_config_from_env(
            apply_safe_mode=False,
            is_canary=canary_flag,
            safe_mode_global=safe_mode_flag,
        )
    except Exception:  # pragma: no cover - defensive
        return snapshot
    snapshot["max_notional_per_venue"] = _serialise_cap_map(cfg.cap_per_venue)
    snapshot["max_notional_per_symbol"] = _serialise_symbol_map(cfg.cap_per_symbol)
    snapshot["daily_loss_limit"] = _as_float(cfg.daily_loss_limit)
    snapshot["extra"] = {
        "daily_cooloff_sec": float(max(cfg.daily_cooloff_sec, 0)),
        "rejects_cooloff_sec": float(max(cfg.rejects_cooloff_sec, 0)),
        "max_consecutive_rejects": float(max(cfg.max_consecutive_rejects, 0)),
    }
    return apply_safe_mode_scaling(snapshot, is_canary=canary_flag, safe_mode_global=safe_mode_flag)


__all__ = [
    "RiskConfig",
    "RiskState",
    "RiskGovernor",
    "load_config_from_env",
    "apply_safe_mode_scaling",
    "get_risk_limits_snapshot",
]
