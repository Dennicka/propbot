"""Risk core scaffold with caps, validation utilities and helpers."""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping as ABCMapping
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

from ..runtime_state_store import load_runtime_payload
from ..services.runtime import get_state


class RiskValidationError(ValueError):
    """Raised when risk limits or inputs are invalid."""


@dataclass(frozen=True)
class RiskCaps:
    """Container for system-wide risk caps.

    The caps are simple positive numeric limits that can be used by other
    services. They intentionally do not contain any orchestration or order
    routing logic – only validation that the configured limits are positive
    numbers.
    """

    max_open_positions: int
    max_total_notional_usdt: float
    max_notional_per_exchange: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_open_positions", self._validate_positive_int("max_open_positions", self.max_open_positions))
        object.__setattr__(self, "max_total_notional_usdt", self._validate_positive_number("max_total_notional_usdt", self.max_total_notional_usdt))
        validated_exchange_caps: Dict[str, float] = {}
        for exchange, cap in self.max_notional_per_exchange.items():
            validated_exchange_caps[exchange] = self._validate_positive_number(
                f"max_notional_per_exchange[{exchange}]", cap
            )
        object.__setattr__(self, "max_notional_per_exchange", validated_exchange_caps)

    @staticmethod
    def _validate_positive_int(name: str, value: int) -> int:
        if value is None or not isinstance(value, int) or value <= 0:
            raise RiskValidationError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _validate_positive_number(name: str, value: float) -> float:
        if value is None:
            raise RiskValidationError(f"{name} must not be None")
        if not isinstance(value, (int, float)):
            raise RiskValidationError(f"{name} must be numeric")
        if value <= 0:
            raise RiskValidationError(f"{name} must be positive")
        return float(value)


class RiskGovernor:
    """Validates exposure metrics against provided :class:`RiskCaps`.

    The governor has no side effects – it merely checks that the supplied
    counts and notionals are non-negative and within the configured limits.
    """

    def __init__(
        self,
        caps: RiskCaps,
        *,
        enforce_open_positions: bool = True,
        enforce_total_notional: bool = True,
    ) -> None:
        self._caps = caps
        self._enforce_open_positions = bool(enforce_open_positions)
        self._enforce_total_notional = bool(enforce_total_notional)

    @property
    def caps(self) -> RiskCaps:
        return self._caps

    def ensure_open_positions_within_limit(self, open_positions: int) -> None:
        if open_positions is None or open_positions < 0:
            raise RiskValidationError("open_positions must be a non-negative integer")
        if open_positions > self._caps.max_open_positions:
            raise RiskValidationError(
                f"open_positions {open_positions} exceeds cap {self._caps.max_open_positions}"
            )

    def ensure_total_notional_within_limit(self, total_notional: float) -> None:
        self._ensure_non_negative_number("total_notional", total_notional)
        if total_notional > self._caps.max_total_notional_usdt:
            raise RiskValidationError(
                f"total_notional {total_notional} exceeds cap {self._caps.max_total_notional_usdt}"
            )

    def ensure_exchange_notional_within_limit(self, exchange: str, exchange_notional: float) -> None:
        self._ensure_non_negative_number(f"exchange_notional[{exchange}]", exchange_notional)
        if exchange not in self._caps.max_notional_per_exchange:
            return
        limit = self._caps.max_notional_per_exchange[exchange]
        if exchange_notional > limit:
            raise RiskValidationError(
                f"exchange_notional {exchange_notional} for {exchange} exceeds cap {limit}"
            )

    @staticmethod
    def _ensure_non_negative_number(name: str, value: float) -> None:
        if value is None:
            raise RiskValidationError(f"{name} must not be None")
        if not isinstance(value, (int, float)):
            raise RiskValidationError(f"{name} must be numeric")
        if value < 0:
            raise RiskValidationError(f"{name} must be non-negative")

    def validate(
        self,
        order_intent: Mapping[str, object] | None,
        *,
        metrics: "_RiskMetrics" | None = None,
    ) -> Dict[str, object | None]:
        """Validate the provided order intent against enforced caps."""

        metrics = metrics or _current_risk_metrics()
        intent_notional = _intent_notional(order_intent)
        intent_positions = _intent_positions(order_intent)

        total_after = max(metrics.total_notional + intent_notional, 0.0)
        open_after = max(metrics.open_positions + intent_positions, 0)

        if self._enforce_total_notional and total_after > self._caps.max_total_notional_usdt:
            return {
                "allowed": False,
                "reason": "risk.max_notional",
                "cap": "max_total_notional_usdt",
                "current_notional": metrics.total_notional,
                "projected_notional": total_after,
                "limit": self._caps.max_total_notional_usdt,
            }

        if self._enforce_open_positions and open_after > self._caps.max_open_positions:
            return {
                "allowed": False,
                "reason": "risk.max_open_positions",
                "cap": "max_open_positions",
                "current_open_positions": metrics.open_positions,
                "projected_open_positions": open_after,
                "limit": self._caps.max_open_positions,
            }

        return {"allowed": True, "reason": "ok", "cap": None}


LOGGER = logging.getLogger(__name__)


class FeatureFlags:
    """Feature flag helpers for the risk core."""

    @staticmethod
    def risk_checks_enabled() -> bool:
        raw = os.getenv("RISK_CHECKS_ENABLED")
        if raw is None:
            return False
        return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class _RiskMetrics:
    open_positions: int = 0
    total_notional: float = 0.0


def _env_int(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _env_float(name: str) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _extract_int(value: object, *, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _extract_float(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _intent_notional(order_intent: Mapping[str, object] | None) -> float:
    if not isinstance(order_intent, ABCMapping):
        return 0.0
    for key in ("intent_notional", "notional_usdt", "notional_usd", "notional"):
        if key in order_intent:
            value = _extract_float(order_intent.get(key))
            if value:
                return max(value, 0.0)
    return 0.0


def _intent_positions(order_intent: Mapping[str, object] | None) -> int:
    if not isinstance(order_intent, ABCMapping):
        return 0
    for key in ("intent_open_positions", "requested_positions", "positions", "open_positions_delta"):
        if key in order_intent:
            value = _extract_int(order_intent.get(key))
            if value:
                return max(value, 0)
    if bool(order_intent.get("opens_position")):
        return 1
    return 0


def _current_risk_metrics() -> _RiskMetrics:
    state = get_state()
    safety = getattr(state, "safety", None)

    runtime_payload = load_runtime_payload()
    positions_payload = runtime_payload.get("positions") if isinstance(runtime_payload, Mapping) else None

    open_positions = 0
    total_notional = 0.0

    risk_snapshot = getattr(safety, "risk_snapshot", {}) if safety is not None else {}
    if isinstance(risk_snapshot, Mapping):
        snapshot_notional = risk_snapshot.get("total_notional_usd")
        if isinstance(snapshot_notional, (int, float)):
            total_notional = float(snapshot_notional)
        per_venue = risk_snapshot.get("per_venue")
        if isinstance(per_venue, Mapping):
            counted = 0
            for payload in per_venue.values():
                if not isinstance(payload, Mapping):
                    continue
                count_value = payload.get("open_positions_count")
                try:
                    counted += int(float(count_value))
                except (TypeError, ValueError):
                    continue
            if counted:
                open_positions = counted

    if open_positions == 0 or total_notional == 0.0:
        if isinstance(positions_payload, list):
            for entry in positions_payload:
                if not isinstance(entry, ABCMapping):
                    continue
                status = str(entry.get("status") or "").lower()
                if status not in {"open", "partial"}:
                    continue
                if bool(entry.get("simulated")):
                    continue
                open_positions += 1
                legs = entry.get("legs")
                if isinstance(legs, list):
                    for leg in legs:
                        if not isinstance(leg, ABCMapping):
                            continue
                        try:
                            leg_notional = float(leg.get("notional_usdt") or 0.0)
                        except (TypeError, ValueError):
                            continue
                        total_notional += abs(leg_notional)
                else:
                    try:
                        total_notional += abs(float(entry.get("notional_usdt") or 0.0))
                    except (TypeError, ValueError):
                        continue

    return _RiskMetrics(open_positions=open_positions, total_notional=total_notional)


_GOVERNOR_SINGLETON: RiskGovernor | None = None
_GOVERNOR_LOCK = threading.RLock()


def _build_risk_governor_from_env() -> RiskGovernor:
    max_open_positions_raw = _env_int("MAX_OPEN_POSITIONS")
    max_total_notional_raw = _env_float("MAX_TOTAL_NOTIONAL_USDT")
    if max_total_notional_raw is None:
        max_total_notional_raw = _env_float("MAX_TOTAL_NOTIONAL_USD")

    enforce_open_positions = bool(max_open_positions_raw)
    enforce_total_notional = bool(max_total_notional_raw)

    caps = RiskCaps(
        max_open_positions=max_open_positions_raw or 1,
        max_total_notional_usdt=max_total_notional_raw or 1.0,
    )
    return RiskGovernor(
        caps,
        enforce_open_positions=enforce_open_positions,
        enforce_total_notional=enforce_total_notional,
    )


def get_risk_governor() -> RiskGovernor:
    """Return a lazily initialised singleton :class:`RiskGovernor`."""

    global _GOVERNOR_SINGLETON
    if _GOVERNOR_SINGLETON is not None:
        return _GOVERNOR_SINGLETON

    with _GOVERNOR_LOCK:
        if _GOVERNOR_SINGLETON is None:
            try:
                _GOVERNOR_SINGLETON = _build_risk_governor_from_env()
            except RiskValidationError as exc:  # pragma: no cover - defensive
                LOGGER.warning("risk governor disabled due to invalid caps", extra={"error": str(exc)})
                caps = RiskCaps(max_open_positions=1, max_total_notional_usdt=1.0)
                _GOVERNOR_SINGLETON = RiskGovernor(
                    caps,
                    enforce_open_positions=False,
                    enforce_total_notional=False,
                )
    return _GOVERNOR_SINGLETON


def reset_risk_governor_for_tests() -> None:
    """Reset the cached governor singleton (useful in tests)."""

    global _GOVERNOR_SINGLETON
    with _GOVERNOR_LOCK:
        _GOVERNOR_SINGLETON = None


def risk_gate(order_intent: Mapping[str, object] | None) -> Dict[str, object | None]:
    """Evaluate whether an order intent is allowed under configured risk caps."""

    if not FeatureFlags.risk_checks_enabled():
        return {"allowed": True, "reason": "disabled", "cap": None}

    governor = get_risk_governor()
    try:
        return governor.validate(order_intent)
    except RiskValidationError as exc:  # pragma: no cover - defensive
        LOGGER.warning("risk gate validation failed", extra={"error": str(exc)})
        return {"allowed": True, "reason": "error", "cap": None}
