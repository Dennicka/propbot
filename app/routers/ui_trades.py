from __future__ import annotations

from typing import Any, Mapping, Optional

from fastapi import APIRouter, HTTPException, Request, status

from ..audit_log import log_operator_action
from ..services.loop import hold_loop
from ..services.runtime import get_state, is_dry_run_mode
from ..services.trades import close_all_trades
from ..utils.operators import OperatorIdentity
from .ui import _authorize_operator_action, _log_operator_success

router = APIRouter(prefix="/api/ui/trades", tags=["ui"])


async def _ensure_auto_trade_disabled() -> bool:
    state = get_state()
    auto_loop_active = bool(getattr(state.control, "auto_loop", False))
    if not auto_loop_active:
        return False
    await hold_loop()
    return True


def _flatten_details(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        payload = {
            "venue": item.get("venue"),
            "symbol": item.get("symbol"),
            "side": item.get("side"),
            "qty": item.get("qty"),
        }
        if "status" in item:
            payload["status"] = item.get("status")
        details.append({key: value for key, value in payload.items() if value is not None})
    return details


@router.post("/close-all")
async def close_all(request: Request) -> dict[str, Any]:
    identity: Optional[OperatorIdentity] = _authorize_operator_action(request, "CLOSE_ALL")
    try:
        auto_loop_disabled = await _ensure_auto_trade_disabled()
        dry_run = is_dry_run_mode()
        result = await close_all_trades(dry_run=dry_run)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="close_all_failed") from exc
    operator_name, role = identity or ("unknown", "unknown")
    closed_items = result.get("closed") if isinstance(result, Mapping) else []
    remaining_items = result.get("positions") if isinstance(result, Mapping) else []
    log_operator_action(
        operator_name,
        role,
        "CLOSE_ALL",
        details={
            "count": len(closed_items) if isinstance(closed_items, list) else 0,
            "positions_remaining": len(remaining_items) if isinstance(remaining_items, list) else 0,
            "dry_run": dry_run,
            "auto_loop_disabled": auto_loop_disabled,
            "closed": _flatten_details(closed_items if isinstance(closed_items, list) else []),
        },
    )
    _log_operator_success(
        identity,
        "CLOSE_ALL",
        extra={
            "count": len(closed_items) if isinstance(closed_items, list) else 0,
            "auto_loop_disabled": auto_loop_disabled,
            "dry_run": dry_run,
        },
    )
    return result if isinstance(result, dict) else {"closed": [], "positions": []}
