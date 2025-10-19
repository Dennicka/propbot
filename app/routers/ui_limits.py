from __future__ import annotations
from fastapi import APIRouter

from ..services.runtime import get_state

router = APIRouter()


@router.get("/limits")
def limits() -> dict:
    state = get_state()
    risk = state.config.data.risk
    guards = state.config.data.guards
    notional = {}
    if risk:
        caps = risk.notional_caps
        notional = {
            "per_symbol_usd": caps.per_symbol_usd,
            "per_venue_usd": caps.per_venue_usd,
            "total_usd": caps.total_usd,
        }
    rate_limits = {}
    if guards:
        rate_limits = {
            "place_per_min": guards.rate_limit.place_per_min,
            "cancel_per_min": guards.rate_limit.cancel_per_min,
        }
    return {
        "risk": {
            "max_day_drawdown_bps": risk.max_day_drawdown_bps if risk else None,
            "notional_caps": notional,
        },
        "rate_limits": rate_limits,
    }
