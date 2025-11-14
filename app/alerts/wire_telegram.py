from __future__ import annotations

import logging
import os
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

ALLOW_NETLOC = {"api.telegram.org"}
ALLOW_SCHEMES = {"https"}

TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")


class TelegramWireError(RuntimeError):
    """Raised when Telegram wire validations or delivery fail."""


def _assert_safe_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOW_SCHEMES:
        raise TelegramWireError(f"Disallowed URL scheme: {parsed.scheme!r}")
    if parsed.netloc not in ALLOW_NETLOC:
        raise TelegramWireError(f"Disallowed host: {parsed.netloc!r}")


def send_message(
    token: str,
    chat_id: str | int,
    text: str,
    *,
    timeout: float = 5.0,
    extra: Mapping[str, Any] | None = None,
) -> int:
    """Send a message through the Telegram Bot API."""

    base = TELEGRAM_API_BASE.rstrip("/")
    url = f"{base}/bot{token}/sendMessage"

    _assert_safe_url(url)

    payload: dict[str, Any] = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if extra:
        payload.update(extra)

    try:
        response = requests.post(url, data=payload, timeout=timeout)
    except requests.RequestException as exc:  # pragma: no cover - network failure
        log.warning("telegram.send_message_error", extra={"error": str(exc)})
        raise TelegramWireError(str(exc)) from exc

    status = response.status_code
    if status >= 400:
        log.warning(
            "telegram.send_message_http_error",
            extra={"status": status, "body": response.text[:500]},
        )
        raise TelegramWireError(f"Non-2xx from Telegram: {status}")

    return status
