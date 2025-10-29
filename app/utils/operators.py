"""Helpers for resolving operator identities from tokens."""
from __future__ import annotations

import os
import secrets
from typing import Optional, Tuple

from ..secrets_store import SecretsStore

OperatorIdentity = Tuple[str, str]


def resolve_operator_identity(token: str) -> Optional[OperatorIdentity]:
    """Resolve an operator identity for ``token`` if possible."""

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


__all__ = ["OperatorIdentity", "resolve_operator_identity"]
