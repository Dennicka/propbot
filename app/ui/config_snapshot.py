from __future__ import annotations

from typing import Any, TypedDict

from app.services.runtime import get_runtime_profile_snapshot
from app.router.smart_router import get_router_control_snapshot
from app.risk.limits import get_risk_limits_snapshot
from app.strategies.registry import get_strategy_registry


class UiConfigSnapshot(TypedDict):
    runtime: dict[str, Any]
    router: dict[str, Any]
    risk_limits: dict[str, Any]
    strategies: dict[str, Any]


def build_ui_config_snapshot() -> UiConfigSnapshot:
    """Assemble a unified runtime configuration snapshot for UI clients."""

    return UiConfigSnapshot(
        runtime=get_runtime_profile_snapshot(),
        router=get_router_control_snapshot(),
        risk_limits=get_risk_limits_snapshot(),
        strategies={
            "items": [
                {
                    "id": info.id,
                    "name": info.name,
                    "tags": list(info.tags),
                    "max_notional_usd": info.max_notional_usd,
                    "max_daily_loss_usd": info.max_daily_loss_usd,
                    "max_open_positions": info.max_open_positions,
                    "enabled": info.enabled,
                    "mode": info.mode,
                    "priority": info.priority,
                }
                for info in get_strategy_registry().all()
            ]
        },
    )
