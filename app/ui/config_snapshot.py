from __future__ import annotations

from typing import Any, TypedDict

from app.services.runtime import get_runtime_profile_snapshot
from app.router.smart_router import get_router_control_snapshot
from app.risk.limits import get_risk_limits_snapshot


class UiConfigSnapshot(TypedDict):
    runtime: dict[str, Any]
    router: dict[str, Any]
    risk_limits: dict[str, Any]


def build_ui_config_snapshot() -> UiConfigSnapshot:
    """Assemble a unified runtime configuration snapshot for UI clients."""

    return UiConfigSnapshot(
        runtime=get_runtime_profile_snapshot(),
        router=get_router_control_snapshot(),
        risk_limits=get_risk_limits_snapshot(),
    )
