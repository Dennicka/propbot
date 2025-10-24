from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Request, status


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_auth_enabled() -> bool:
    return _truthy(os.getenv("AUTH_ENABLED"))


def _expected_token() -> str | None:
    return os.getenv("API_TOKEN")


def require_token(request: Request) -> None:
    if not is_auth_enabled():
        return
    expected_token = _expected_token()
    if not expected_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    if not secrets.compare_digest(token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
