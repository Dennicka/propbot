"""Utilities for loading secrets from a JSON secrets store."""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
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
        self._encryption_key = os.environ.get("SECRETS_ENC_KEY")

    def _load(self) -> Dict[str, object]:
        if not self._path.exists():
            raise FileNotFoundError(f"Secrets store file not found: {self._path}")

        with self._path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _operator_entries(self) -> Tuple[Tuple[str, Optional[str], Optional[str]], ...]:
        operators = self._data.get("operator_tokens", {})
        if not isinstance(operators, dict):
            return tuple()

        entries = []
        for name, payload in operators.items():
            if not isinstance(payload, dict):
                continue
            token = payload.get("token")
            role = payload.get("role")
            entries.append(
                (
                    name,
                    token if isinstance(token, str) else None,
                    role if isinstance(role, str) else None,
                )
            )
        return tuple(entries)

    def get_operator_by_token(self, token: str) -> Optional[Tuple[str, str]]:
        """Return ``(operator_name, role)`` for the provided token.

        If the token is unknown ``None`` is returned.
        """

        return self.get_operator_info_by_token(token)

    def get_operator_info_by_token(self, token: str) -> Optional[Tuple[str, str]]:
        """Return the operator name and role for ``token`` without exposing secrets."""

        for name, stored_token, role in self._operator_entries():
            if stored_token and stored_token == token and role:
                return name, role
        return None

    def list_operator_infos(self) -> Tuple[Tuple[str, str], ...]:
        """Return all operators as ``(name, role)`` tuples without exposing tokens."""

        infos = [(name, role) for name, _token, role in self._operator_entries() if role]
        return tuple(infos)

    def decrypt_secret(self, value: Optional[str]) -> Optional[str]:
        """Decrypt ``value`` using the configured encryption key.

        The secrets store uses a base64-wrapped XOR cipher as a placeholder. If
        ``SECRETS_ENC_KEY`` is not set, ``value`` is returned as-is.
        """

        if not value or not isinstance(value, str):
            return value

        if not self._encryption_key:
            return value

        try:
            payload = base64.b64decode(value)
        except Exception:
            return None

        key_bytes = self._encryption_key.encode("utf-8")
        if not key_bytes:
            return None

        decrypted = bytes(
            byte ^ key_bytes[index % len(key_bytes)] for index, byte in enumerate(payload)
        )
        try:
            return decrypted.decode("utf-8")
        except UnicodeDecodeError:
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
            key = self.decrypt_secret(self._data.get(f"{exchange}_key"))
            secret = self.decrypt_secret(self._data.get(f"{exchange}_secret"))
            exchanges[exchange] = {
                "key": key if isinstance(key, str) else None,
                "secret": secret if isinstance(secret, str) else None,
            }
        return exchanges

    def _rotation_metadata(self) -> Dict[str, str]:
        meta = self._data.get("meta", {})
        if isinstance(meta, dict):
            return {key: value for key, value in meta.items() if isinstance(value, str)}
        return {}

    def needs_rotation(self, threshold_days: int) -> Dict[str, bool]:
        """Return a mapping of exchange keys that require rotation."""

        meta = self._rotation_metadata()
        now = datetime.now(timezone.utc)
        threshold = max(threshold_days, 0)
        result: Dict[str, bool] = {}
        thresholds = {
            "binance_key": meta.get("binance_key_last_rotated"),
            "okx_key": meta.get("okx_key_last_rotated"),
        }

        for name, timestamp in thresholds.items():
            requires_rotation = False
            if timestamp:
                try:
                    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    parsed = None
                if parsed:
                    delta = now - parsed
                    requires_rotation = delta.days >= threshold
            result[name] = requires_rotation
        return result


__all__ = ["SecretsStore"]
