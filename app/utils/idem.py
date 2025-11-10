from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.security import require_token

DEFAULT_IDEMPOTENCY_TTL = 600


@dataclass
class CachedResponse:
    status_code: int
    headers: list[tuple[str, str]]
    body: bytes
    expires_at: float
    fingerprint: str


class IdempotencyKeyConflict(Exception):
    """Raised when an idempotency key is reused with a different payload."""


class IdempotencyCache:
    def __init__(
        self,
        ttl_seconds: int | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_seconds is None:
            ttl_seconds = int(os.getenv("IDEM_TTL_SEC", DEFAULT_IDEMPOTENCY_TTL))
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        self._ttl = ttl_seconds
        self._clock = clock or time.monotonic
        self._entries: dict[str, CachedResponse] = {}
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def set_ttl(self, ttl_seconds: int) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        with self._lock:
            self._ttl = ttl_seconds

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _now(self) -> float:
        return self._clock()

    def _prune(self) -> None:
        now = self._now()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)

    def get(self, key: str, fingerprint: str) -> CachedResponse | None:
        with self._lock:
            self._prune()
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.fingerprint != fingerprint:
                raise IdempotencyKeyConflict("Idempotency payload mismatch")
            if entry.expires_at <= self._now():
                self._entries.pop(key, None)
                return None
            return entry

    def store(
        self,
        key: str,
        fingerprint: str,
        *,
        status_code: int,
        headers: Iterable[tuple[str, str]],
        body: bytes,
    ) -> None:
        expires_at = self._now() + self._ttl
        header_items = [
            (name, value) for name, value in headers if name.lower() != "idempotent-replay"
        ]
        with self._lock:
            self._prune()
            self._entries[key] = CachedResponse(
                status_code=status_code,
                headers=header_items,
                body=body,
                expires_at=expires_at,
                fingerprint=fingerprint,
            )

    @staticmethod
    def build_fingerprint(
        method: str,
        path: str,
        body: bytes,
        content_type: str | None,
    ) -> str:
        normalized_body = IdempotencyCache._normalize_body(body, content_type)
        digest = hashlib.sha256()
        digest.update(method.upper().encode("utf-8"))
        digest.update(b"::")
        digest.update(path.encode("utf-8"))
        digest.update(b"::")
        digest.update(normalized_body)
        return digest.hexdigest()

    @staticmethod
    def _normalize_body(body: bytes, content_type: str | None) -> bytes:
        if not body:
            return b""
        if content_type and "application/json" in content_type.lower():
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return body
            normalized = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
            return normalized.encode("utf-8")
        return body


def _guard_request(request: Request, should_guard: Callable[[Request], bool]) -> bool:
    try:
        return should_guard(request)
    except Exception:  # pragma: no cover - defensive guard hook
        return False


class IdempotencyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        cache: IdempotencyCache,
        should_guard: Callable[[Request], bool],
    ) -> None:
        super().__init__(app)
        self._cache = cache
        self._should_guard = should_guard

    async def dispatch(self, request: Request, call_next):
        if not _guard_request(request, self._should_guard):
            return await call_next(request)

        try:
            require_token(request)
        except HTTPException as exc:
            headers = dict(exc.headers or {})
            return JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=headers
            )

        key = request.headers.get("Idempotency-Key")
        fingerprint: str | None = None
        if key:
            body = await request.body()
            fingerprint = self._cache.build_fingerprint(
                request.method,
                request.url.path,
                body,
                request.headers.get("content-type"),
            )
            try:
                cached = self._cache.get(key, fingerprint)
            except IdempotencyKeyConflict:
                return JSONResponse(
                    {"detail": "idempotency key conflict"},
                    status_code=status.HTTP_409_CONFLICT,
                )
            if cached is not None:
                headers = {name: value for name, value in cached.headers}
                headers["Idempotent-Replay"] = "true"
                return Response(
                    content=cached.body,
                    status_code=cached.status_code,
                    headers=headers,
                    media_type=None,
                )

        response = await call_next(request)

        response_body = b""
        async for chunk in response.body_iterator:
            response_body += chunk

        new_response = Response(
            content=response_body,
            status_code=response.status_code,
            media_type=response.media_type,
            background=response.background,
        )
        for name, value in response.headers.items():
            new_response.headers[name] = value

        if key and fingerprint is not None:
            self._cache.store(
                key,
                fingerprint,
                status_code=response.status_code,
                headers=response.headers.items(),
                body=response_body,
            )
        return new_response
