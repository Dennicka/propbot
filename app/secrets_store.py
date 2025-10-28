"""Utilities for loading secrets from a JSON secrets store."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple


class SecretsStore:
    """Access secrets stored in a JSON file.

    The file path is read from the ``SECRETS_STORE_PATH`` environment variable by
    default. Callers may provide an explicit path during construction for tests.
    """

    def __init__(self, secrets_path: Optional[str] = None) -> None:
        path = secrets_path or os.environ.get("SECRETS_STORE_PATH")
        if not path:
            raise ValueError("SECRETS_STORE_PATH is not set")

        self._path = Path(path).expanduser()
        self._data = self._load()

    def _load(self) -> Dict[str, object]:
        if not self._path.exists():
            raise FileNotFoundError(f"Secrets store file not found: {self._path}")

        with self._path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def get_operator_by_token(self, token: str) -> Optional[Tuple[str, str]]:
        """Return ``(operator_name, role)`` for the provided token.

        If the token is unknown ``None`` is returned.
        """

        operators = self._data.get("operator_tokens", {})
        if not isinstance(operators, dict):
            return None

        for name, payload in operators.items():
            if not isinstance(payload, dict):
                continue
            if payload.get("token") == token:
                role = payload.get("role")
                if isinstance(role, str):
                    return name, role
        return None

    def get_approve_token(self) -> Optional[str]:
        """Return the token used to approve privileged operations."""

        token = self._data.get("approve_token")
        return token if isinstance(token, str) else None

    def get_exchange_keys(self) -> Dict[str, Dict[str, Optional[str]]]:
        """Return the exchange API credentials that are available.

        The structure mirrors the base exchange names from the secrets store.
        Each entry maps to a ``{"key": str | None, "secret": str | None}``.
        """

        exchanges: Dict[str, Dict[str, Optional[str]]] = {}
        for exchange in ("binance", "okx"):
            key = self._data.get(f"{exchange}_key")
            secret = self._data.get(f"{exchange}_secret")
            exchanges[exchange] = {
                "key": key if isinstance(key, str) else None,
                "secret": secret if isinstance(secret, str) else None,
            }
        return exchanges


__all__ = ["SecretsStore"]
