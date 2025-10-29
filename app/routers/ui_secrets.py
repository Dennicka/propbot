from __future__ import annotations

import os
import secrets
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, status

from ..audit_log import log_operator_action
from ..secrets_store import SecretsStore
from ..security import is_auth_enabled, require_token


router = APIRouter(prefix="/api/ui/secrets", tags=["ui"])


def _resolve_identity(token: Optional[str], store: SecretsStore) -> Optional[Tuple[str, str]]:
    if token is None:
        if is_auth_enabled():
            return None
        return ("system", "operator")

    identity = store.get_operator_info_by_token(token)
    if identity:
        return identity

    expected_token = os.getenv("API_TOKEN")
    if expected_token and secrets.compare_digest(token, expected_token):
        return ("api", "operator")
    return None


@router.get("/status")
def secrets_status(request: Request, threshold_days: int = 90) -> Dict[str, object]:
    token = require_token(request)
    try:
        store = SecretsStore()
    except Exception as exc:  # pragma: no cover - defensive guard
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="secrets_store_unavailable",
        ) from exc

    identity = _resolve_identity(token, store)
    if not identity:
        log_operator_action("unknown", "unknown", "SECRETS_STATUS", channel="api", details="forbidden")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    name, role = identity
    if role != "operator":
        log_operator_action(name, role, "SECRETS_STATUS", channel="api", details="forbidden")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    rotation = store.needs_rotation(threshold_days)
    operators = [
        {"name": operator_name, "role": operator_role}
        for operator_name, operator_role in store.list_operator_infos()
    ]

    log_operator_action(name, role, "SECRETS_STATUS", channel="api", details="ok")
    return {"rotation_needed": rotation, "operators": operators}


__all__ = ["router"]
