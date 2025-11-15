from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal, Sequence

from app.metrics.live_guard import live_trading_guard_state

logger = logging.getLogger(__name__)

RuntimeProfileName = str  # "paper", "testnet.binance", "live"
VenueId = str
StrategyId = str | None

LiveGuardState = Literal["disabled", "enabled", "test_only"]


@dataclass(slots=True)
class LiveGuardConfigView:
    """Snapshot of current live-trading guard configuration."""

    runtime_profile: RuntimeProfileName
    state: LiveGuardState
    allow_live_trading: bool
    allowed_venues: Sequence[VenueId]
    allowed_strategies: Sequence[str]
    reason: str | None = None


class LiveTradingDisabledError(RuntimeError):
    """Raised when code attempts to trade live while live trading is disabled."""


class LiveTradingGuard:
    """Central guard that decides if live trading is currently allowed."""

    def __init__(
        self,
        runtime_profile: RuntimeProfileName,
        *,
        allow_live_env_var: str = "ALLOW_LIVE_TRADING",
        allowed_venues_env_var: str = "LIVE_TRADING_ALLOWED_VENUES",
        allowed_strategies_env_var: str = "LIVE_TRADING_ALLOWED_STRATEGIES",
    ) -> None:
        self._runtime_profile = runtime_profile
        self._allow_live_env_var = allow_live_env_var
        self._allowed_venues_env_var = allowed_venues_env_var
        self._allowed_strategies_env_var = allowed_strategies_env_var

    def _parse_bool(self, value: str | None) -> bool:
        if value is None:
            return False
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def _parse_list(self, value: str | None) -> list[str]:
        if not value:
            return []
        return [item.strip() for item in value.split(",") if item.strip()]

    def _compute_state(self) -> LiveGuardConfigView:
        profile = self._runtime_profile

        env_allow = self._parse_bool(os.getenv(self._allow_live_env_var))
        allowed_venues = self._parse_list(os.getenv(self._allowed_venues_env_var))
        allowed_strategies = self._parse_list(os.getenv(self._allowed_strategies_env_var))

        if profile.startswith("paper") or profile.startswith("testnet"):
            return LiveGuardConfigView(
                runtime_profile=profile,
                state="test_only",
                allow_live_trading=False,
                allowed_venues=[],
                allowed_strategies=[],
                reason="Runtime profile is not live (paper/testnet only)",
            )

        if not env_allow:
            return LiveGuardConfigView(
                runtime_profile=profile,
                state="disabled",
                allow_live_trading=False,
                allowed_venues=[],
                allowed_strategies=[],
                reason=f"{self._allow_live_env_var}=false or unset",
            )

        return LiveGuardConfigView(
            runtime_profile=profile,
            state="enabled",
            allow_live_trading=True,
            allowed_venues=allowed_venues,
            allowed_strategies=allowed_strategies,
            reason=None,
        )

    def _record_state_metric(self, cfg: LiveGuardConfigView) -> None:
        state_val = {"test_only": 0, "disabled": 1, "enabled": 2}[cfg.state]
        try:
            live_trading_guard_state.labels(runtime_profile=cfg.runtime_profile).set(state_val)
        except Exception as exc:  # pragma: no cover - defensive metrics update
            logger.warning(
                "Failed to update live_trading_guard_state metric for profile %s: %s",
                getattr(cfg, "runtime_profile", "unknown"),
                exc,
            )

    def get_config_view(self) -> LiveGuardConfigView:
        """Return current configuration snapshot for UI/metrics."""
        cfg = self._compute_state()
        self._record_state_metric(cfg)
        return cfg

    def ensure_live_allowed(
        self,
        *,
        venue_id: VenueId,
        strategy_id: StrategyId,
    ) -> None:
        """Raise if live trading is currently not allowed for given venue/strategy."""
        cfg = self._compute_state()

        if cfg.state == "test_only":
            raise LiveTradingDisabledError(
                f"Attempted live trading in non-live profile: {cfg.runtime_profile}"
            )

        if not cfg.allow_live_trading:
            raise LiveTradingDisabledError(
                cfg.reason or "Live trading is disabled by configuration."
            )

        if cfg.allowed_venues and venue_id not in cfg.allowed_venues:
            raise LiveTradingDisabledError(
                f"Venue {venue_id} is not in LIVE_TRADING_ALLOWED_VENUES."
            )

        if cfg.allowed_strategies:
            if strategy_id is None or strategy_id not in cfg.allowed_strategies:
                raise LiveTradingDisabledError(
                    "Strategy is not allowed for live trading (check LIVE_TRADING_ALLOWED_STRATEGIES)."
                )
