"""Utilities for resolving operator identities and roles."""

from __future__ import annotations

import os
import secrets
from typing import Optional, Tuple

from ..secrets_store import SecretsStore

OperatorIdentity = Tuple[str, str]


def resolve_operator_identity(token: str) -> Optional[OperatorIdentity]:
    """Return ``(name, role)`` for ``token`` if it matches a known operator."""

    store: Optional[SecretsStore]
    try:
        store = SecretsStore()
    except Exception:
        store = None
    if store:
        identity = store.get_operator_by_token(token)
        if identity:
            name, role = identity
            return (str(name), str(role))
    expected_token = os.getenv("API_TOKEN")
    if expected_token and secrets.compare_digest(token, expected_token):
        return ("api", "operator")
    return None


__all__ = ["OperatorIdentity", "resolve_operator_identity"]
