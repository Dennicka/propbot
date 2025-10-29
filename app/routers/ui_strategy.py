"""Strategy orchestrator API endpoints."""

import os
import secrets
from typing import Any, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..audit_log import log_operator_action
from ..capital_manager import get_capital_manager
from ..secrets_store import SecretsStore
from ..security import require_token
from ..strategy_orchestrator import get_strategy_orchestrator


router = APIRouter(prefix="/strategy")


class StrategyTogglePayload(BaseModel):
    strategy: str = Field(..., min_length=1, description="Strategy identifier")
    reason: str = Field(..., min_length=1, description="Operator supplied reason")


OperatorIdentity = Tuple[str, str]


def _resolve_operator_identity(token: str) -> Optional[OperatorIdentity]:
    store: Optional[SecretsStore]
    try:
        store = SecretsStore()
    except Exception:
        store = None
    if store:
        identity = store.get_operator_by_token(token)
        if identity:
            return identity
    expected_token = os.getenv("API_TOKEN")
    if expected_token and secrets.compare_digest(token, expected_token):
        return ("api", "operator")
    return None


def _require_operator(request: Request, action: str) -> OperatorIdentity:
    token = require_token(request)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    identity = _resolve_operator_identity(token)
    if not identity:
        log_operator_action(
            "unknown",
            "unknown",
            action,
            details={"status": "forbidden"},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    name, role = identity
    if role != "operator":
        log_operator_action(
            name,
            role,
            action,
            details={"status": "forbidden"},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return identity


@router.post("/enable")
def enable_strategy(payload: StrategyTogglePayload, request: Request) -> dict[str, Any]:
    operator_name, role = _require_operator(request, action="enable_strategy")
    orchestrator = get_strategy_orchestrator()
    orchestrator.enable_strategy(
        payload.strategy,
        payload.reason,
        operator=operator_name,
        role=role,
    )
    return orchestrator.snapshot()


@router.post("/disable")
def disable_strategy(payload: StrategyTogglePayload, request: Request) -> dict[str, Any]:
    operator_name, role = _require_operator(request, action="disable_strategy")
    orchestrator = get_strategy_orchestrator()
    orchestrator.disable_strategy(
        payload.strategy,
        payload.reason,
        operator=operator_name,
        role=role,
    )
    return orchestrator.snapshot()


@router.get("/status")
def strategy_status(request: Request) -> dict[str, Any]:
    require_token(request)
    orchestrator = get_strategy_orchestrator()
    manager = get_capital_manager()
    capital_snapshot = manager.snapshot()
    headroom = capital_snapshot.get("headroom", {})
    return {
        "orchestrator": orchestrator.snapshot(),
        "capital_headroom": headroom,
    }
