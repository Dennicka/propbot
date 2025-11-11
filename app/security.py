from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

from fastapi import HTTPException, Request, status

from .secrets_store import SecretsStore


LOGGER = logging.getLogger(__name__)


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_auth_enabled() -> bool:
    return _truthy(os.getenv("AUTH_ENABLED"))


def _expected_token() -> str | None:
    return os.getenv("API_TOKEN")


def _extract_bearer_token(request: Request) -> str:
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    return token


def _load_secrets_store() -> Optional[SecretsStore]:
    try:
        return SecretsStore()
    except Exception as exc:
        LOGGER.error(
            "failed to load secrets store", extra={"error": str(exc)}, exc_info=True
        )
        return None


def require_token(request: Request) -> Optional[str]:
    if not is_auth_enabled():
        return None
    token = _extract_bearer_token(request)
    expected_token = _expected_token()
    if expected_token and secrets.compare_digest(token, expected_token):
        return token
    store = _load_secrets_store()
    if store and store.get_operator_by_token(token):
        return token
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
