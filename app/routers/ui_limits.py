from __future__ import annotations
from fastapi import APIRouter

router = APIRouter()

@router.get("/limits")
def limits() -> dict:
    return {
        "risk": {
            "max_day_drawdown_bps": 300,
            "notional_caps": {"BINANCE": 0, "OKX": 0, "BYBIT": 0}
        },
        "rate_limits": {
            "place_per_min": 300,
            "cancel_per_min": 600
        }
    }
