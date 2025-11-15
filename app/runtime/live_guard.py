from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Sequence

from app.approvals.live_toggle import (
    LiveToggleAction,
    LiveToggleStatus,
    get_live_toggle_store,
)
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
    approvals_enabled: bool | None = None
    approvals_last_request_id: str | None = None
    approvals_last_action: LiveToggleAction | None = None
    approvals_last_status: LiveToggleStatus | None = None
    approvals_last_updated_at: datetime | None = None
    approvals_requestor_id: str | None = None
    approvals_approver_id: str | None = None
    approvals_resolution_reason: str | None = None


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
        approvals_state = get_live_toggle_store().get_effective_state()

        def _build_config(
            *,
            state: LiveGuardState,
            allow_live: bool,
            allowed_venues_value: Sequence[str],
            allowed_strategies_value: Sequence[str],
            reason: str | None,
        ) -> LiveGuardConfigView:
            return LiveGuardConfigView(
                runtime_profile=profile,
                state=state,
                allow_live_trading=allow_live,
                allowed_venues=allowed_venues_value,
                allowed_strategies=allowed_strategies_value,
                reason=reason,
                approvals_enabled=approvals_state.enabled,
                approvals_last_request_id=approvals_state.last_request_id,
                approvals_last_action=approvals_state.last_action,
                approvals_last_status=approvals_state.last_status,
                approvals_last_updated_at=approvals_state.last_updated_at,
                approvals_requestor_id=approvals_state.requestor_id,
                approvals_approver_id=approvals_state.approver_id,
                approvals_resolution_reason=approvals_state.resolution_reason,
            )

        if profile.startswith("paper") or profile.startswith("testnet"):
            return _build_config(
                state="test_only",
                allow_live=False,
                allowed_venues_value=[],
                allowed_strategies_value=[],
                reason="Runtime profile is not live (paper/testnet only)",
            )

        if not env_allow:
            return _build_config(
                state="disabled",
                allow_live=False,
                allowed_venues_value=[],
                allowed_strategies_value=[],
                reason=f"{self._allow_live_env_var}=false or unset",
            )

        if not approvals_state.enabled:
            return _build_config(
                state="disabled",
                allow_live=False,
                allowed_venues_value=allowed_venues,
                allowed_strategies_value=allowed_strategies,
                reason="two-man approvals not granted",
            )

        return _build_config(
            state="enabled",
            allow_live=True,
            allowed_venues_value=allowed_venues,
            allowed_strategies_value=allowed_strategies,
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
