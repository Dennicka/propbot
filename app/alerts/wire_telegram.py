from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Sequence

LOGGER = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org"


def _build_url(token: str) -> str:
    return f"{_API_URL}/bot{token}/sendMessage"


def send_message(
    *,
    token: str,
    chat_id: str,
    text: str,
    timeout: int,
    retries: Sequence[int],
) -> bool:
    if not token or not chat_id:
        LOGGER.warning("telegram.send_message_missing_credentials")
        return False
    url = _build_url(token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }
    encoded = urllib.parse.urlencode(payload).encode("utf-8")
    delays = [0, *list(retries)]
    for delay in delays:
        if delay > 0:
            time.sleep(delay)
        try:
            request = urllib.request.Request(url, data=encoded, method="POST")  # noqa: S310
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(request, timeout=timeout) as response:  # type: ignore[arg-type]  # noqa: S310
                status = getattr(response, "status", 200)
                if 200 <= status < 300:
                    return True
                if status < 500:
                    LOGGER.warning("telegram.send_message_http_error", extra={"status": status})
                    return False
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status < 500:
                LOGGER.warning("telegram.send_message_http_error", extra={"status": status})
                return False
        except (urllib.error.URLError, TimeoutError) as exc:
            LOGGER.warning(
                "telegram.send_message_error",
                extra={"error": getattr(exc, "reason", str(exc))},
                exc_info=True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("telegram.send_message_unhandled")
            return False
    return False
