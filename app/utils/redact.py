"""Utilities for sanitising API payloads before returning them to clients."""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping, Sequence

REDACTED = "***redacted***"

# Environment variables that may contain API keys, tokens or chat identifiers.
_SECRET_ENV_VARS = (
    "API_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "BINANCE_UM_API_KEY_TESTNET",
    "BINANCE_UM_API_SECRET_TESTNET",
    "BINANCE_LV_API_KEY",
    "BINANCE_LV_API_SECRET",
    "BINANCE_LV_API_KEY_TESTNET",
    "BINANCE_LV_API_SECRET_TESTNET",
    "OKX_API_KEY_TESTNET",
    "OKX_API_SECRET_TESTNET",
    "OKX_API_PASSPHRASE_TESTNET",
)


def _gather_secret_values(extra: Iterable[str] | None = None) -> tuple[str, ...]:
    values: list[str] = []
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value:
            values.append(str(value))
    if extra:
        values.extend(str(item) for item in extra if item)
    return tuple(values)


def _redact_string(value: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret and secret in value:
            return REDACTED
    return value


def _redact_iterable(values: Iterable[Any], secrets: Sequence[str]) -> Iterable[Any]:
    if isinstance(values, tuple):
        return tuple(_redact_payload(item, secrets) for item in values)
    if isinstance(values, set):
        return {_redact_payload(item, secrets) for item in values}
    return [_redact_payload(item, secrets) for item in values]


def _redact_mapping(values: Mapping[Any, Any], secrets: Sequence[str]) -> Mapping[Any, Any]:
    return {key: _redact_payload(val, secrets) for key, val in values.items()}


def _redact_payload(payload: Any, secrets: Sequence[str]) -> Any:
    if isinstance(payload, str):
        return _redact_string(payload, secrets)
    if isinstance(payload, Mapping):
        return _redact_mapping(payload, secrets)
    if isinstance(payload, (list, tuple, set)):
        return _redact_iterable(payload, secrets)
    return payload


def redact_sensitive_data(payload: Any, *, extra_secrets: Iterable[str] | None = None) -> Any:
    """Return a copy of *payload* with secrets replaced by ``***redacted***``.

    The function walks nested mappings/lists/sets and scrubs any string values
    that match secrets discovered in the environment or provided via
    ``extra_secrets``.
    """

    secrets = _gather_secret_values(extra_secrets)
    if not secrets:
        return payload
    return _redact_payload(payload, secrets)
