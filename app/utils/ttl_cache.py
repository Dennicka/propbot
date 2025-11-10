"""FastAPI response caching helpers with TTL and conditional requests."""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import timezone
from typing import Any, TypedDict

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from ..metrics.cache import record_cache_observation


LOGGER = logging.getLogger(__name__)


def _make_etag(body: bytes) -> str:
    """Create a deterministic, collision-resistant ETag for cached content."""
    try:
        digest = hashlib.blake2b(body, digest_size=16)
    except TypeError:  # pragma: no cover - legacy Python without keyword-only args
        digest = hashlib.blake2b(body, 16)  # type: ignore[arg-type]
    return digest.hexdigest()


CacheVary = Callable[[Request, tuple[Any, ...], dict[str, Any]], Any]


class _CacheEntry(TypedDict):
    status: int
    headers: dict[str, str]
    body: bytes
    created_ts: float
    expires_ts: float
    etag: str
    last_modified_ts: float
    media_type: str | None


_CACHE: dict[str, _CacheEntry] = {}
_LOCK = threading.Lock()


def _httpdate(timestamp: float) -> str:
    from email.utils import formatdate

    return formatdate(timestamp, usegmt=True)


def _parse_httpdate(value: str) -> float | None:
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.timestamp()


def is_test_env() -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST")) or os.getenv("CI_TESTING") == "1"


def _build_cache_key(request: Request) -> str:
    method = request.method.upper()
    path = request.url.path
    items = sorted(request.query_params.multi_items())
    if items:
        query = "&".join(f"{key}={value}" for key, value in items)
        base = f"{method} {path}?{query}"
    else:
        base = f"{method} {path}"
    if is_test_env():
        marker = os.getenv("PYTEST_CURRENT_TEST")
        if marker:
            base = f"{base}|pytest:{marker}"
    return base


def _extend_key_with_vary(
    base_key: str,
    request: Request,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    vary: CacheVary | None,
) -> str:
    if vary is None:
        return base_key
    extra = vary(request, args, kwargs)
    if extra is None:
        return base_key
    parts = [base_key]
    if isinstance(extra, Iterable) and not isinstance(extra, (str, bytes)):
        parts.extend(str(item) for item in extra)
    else:
        parts.append(str(extra))
    return "|".join(parts)


def _record_hit(endpoint: str) -> None:
    try:
        record_cache_observation(endpoint, True)
    except Exception as exc:  # pragma: no cover - defensive  # noqa: BLE001
        LOGGER.debug(
            "failed to record cache hit",
            extra={"endpoint": endpoint},
            exc_info=exc,
        )


def _conditional_headers(entry: _CacheEntry) -> dict[str, str]:
    headers = {
        "ETag": entry["etag"],
        "Last-Modified": _httpdate(entry["last_modified_ts"]),
    }
    cache_control = entry["headers"].get("Cache-Control")
    if cache_control:
        headers["Cache-Control"] = cache_control
    return headers


def _clock() -> float:
    try:
        from ..services import cache as data_cache
    except Exception as exc:  # pragma: no cover - fallback when cache not available  # noqa: BLE001
        LOGGER.debug("cache service unavailable, using monotonic clock", exc_info=exc)
        return time.monotonic()
    monotonic_fn = getattr(data_cache, "_monotonic", None)
    if callable(monotonic_fn):
        try:
            return float(monotonic_fn())
        except Exception as exc:  # pragma: no cover - defensive  # noqa: BLE001
            LOGGER.debug("cache monotonic provider failed", exc_info=exc)
            return time.monotonic()
    return time.monotonic()


def _get_entry(key: str, now: float) -> _CacheEntry | None:
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        if entry["expires_ts"] <= now:
            _CACHE.pop(key, None)
            return None
        return entry


def _set_entry(key: str, entry: _CacheEntry) -> None:
    with _LOCK:
        _CACHE[key] = entry


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _extract_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request | None:
    for value in args:
        if isinstance(value, Request):
            return value
    for value in kwargs.values():
        if isinstance(value, Request):
            return value
    return None


async def _ensure_response(result: Any) -> Response:
    if isinstance(result, Response):
        return result
    if isinstance(result, (bytes, bytearray)):
        return Response(content=bytes(result))
    if isinstance(result, str):
        return Response(content=result)
    return JSONResponse(content=result)


async def _read_response_body(resp: Response) -> bytes:
    body = resp.body
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode(resp.charset or "utf-8")
    to_render = getattr(resp, "content", b"")
    rendered = resp.render(to_render)
    if isinstance(rendered, (bytes, bytearray)):
        return bytes(rendered)
    if isinstance(rendered, str):
        return rendered.encode(resp.charset or "utf-8")
    return bytes(rendered)


def _matches_if_none_match(request: Request, entry: _CacheEntry) -> bool:
    header_value = request.headers.get("if-none-match")
    if not header_value:
        return False
    candidates = [token.strip() for token in header_value.split(",") if token.strip()]
    return "*" in candidates or entry["etag"] in candidates


def _matches_if_modified_since(request: Request, entry: _CacheEntry) -> bool:
    header_value = request.headers.get("if-modified-since")
    if not header_value:
        return False
    parsed_ts = _parse_httpdate(header_value)
    if parsed_ts is None:
        return False
    return parsed_ts >= entry["last_modified_ts"]


def _build_cached_response(entry: _CacheEntry) -> Response:
    headers = dict(entry["headers"])
    return Response(
        status_code=entry["status"],
        content=entry["body"],
        headers=headers,
        media_type=entry["media_type"],
    )


def cache_response(
    ttl_s: float,
    *,
    allow_in_tests: bool = True,
    vary: CacheVary | None = None,
    refresh_on_hit: bool = False,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Response]]]:
    """Cache FastAPI handler responses with a TTL and 304 support."""

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Response]]:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> Response:
            request = _extract_request(args, kwargs)
            if request is None:
                result = await fn(*args, **kwargs)
                return await _ensure_response(result)

            if is_test_env() and not allow_in_tests:
                result = await fn(*args, **kwargs)
                return await _ensure_response(result)

            clock_now = _clock()
            base_key = _build_cache_key(request)
            cache_key = _extend_key_with_vary(base_key, request, args, kwargs, vary)
            entry = _get_entry(cache_key, clock_now)
            if entry is not None:
                _record_hit(request.url.path)
                if not refresh_on_hit:
                    if _matches_if_none_match(request, entry) or _matches_if_modified_since(
                        request, entry
                    ):
                        return Response(status_code=304, headers=_conditional_headers(entry))
                    return _build_cached_response(entry)
            else:
                try:
                    record_cache_observation(request.url.path, False)
                except Exception as exc:  # pragma: no cover - defensive  # noqa: BLE001
                    LOGGER.debug(
                        "failed to record cache miss",
                        extra={"endpoint": request.url.path},
                        exc_info=exc,
                    )

            result = await fn(*args, **kwargs)
            response = await _ensure_response(result)
            body = await _read_response_body(response)
            etag = _make_etag(body)
            headers = dict(response.headers)
            cache_control_value = None
            for key, value in list(headers.items()):
                if key.lower() == "cache-control":
                    cache_control_value = value
                    del headers[key]
                    break
            if cache_control_value is None:
                cache_control_value = f"public, max-age={int(ttl_s)}"
            headers["Cache-Control"] = cache_control_value
            last_modified_ts = time.time()
            headers["ETag"] = etag
            headers["Last-Modified"] = _httpdate(last_modified_ts)

            entry = _CacheEntry(
                status=response.status_code,
                headers=headers,
                body=body,
                created_ts=clock_now,
                expires_ts=clock_now + ttl_s,
                etag=etag,
                last_modified_ts=last_modified_ts,
                media_type=response.media_type,
            )
            _set_entry(cache_key, entry)
            if _matches_if_none_match(request, entry) or _matches_if_modified_since(request, entry):
                return Response(status_code=304, headers=_conditional_headers(entry))
            return Response(
                status_code=response.status_code,
                content=body,
                headers=headers,
                media_type=response.media_type,
            )

        return inner

    return decorator


__all__ = ["cache_response", "is_test_env", "CacheVary", "clear_cache"]
