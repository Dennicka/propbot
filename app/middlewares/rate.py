from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

DEFAULT_RATE_PER_MIN = 30
DEFAULT_BURST = 10


def _guard_request(request: Request, should_guard: Callable[[Request], bool]) -> bool:
    try:
        return should_guard(request)
    except Exception:  # pragma: no cover - defensive guard hook
        return False


@dataclass
class _TokenBucket:
    tokens: float
    updated_at: float


@dataclass
class RateLimitOutcome:
    allowed: bool
    remaining_tokens: float
    reset_seconds: float


class RateLimiter:
    def __init__(
        self,
        rate_per_min: int | None = None,
        burst: int | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if rate_per_min is None:
            rate_per_min = int(os.getenv("API_RATE_PER_MIN", DEFAULT_RATE_PER_MIN))
        if burst is None:
            burst = int(os.getenv("API_BURST", DEFAULT_BURST))
        self._lock = threading.Lock()
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _TokenBucket] = {}
        self._rate_per_min = 1
        self._burst = 1
        self._refill_rate = 1.0
        self.set_limits(rate_per_min, burst)

    @property
    def rate_per_min(self) -> int:
        return self._rate_per_min

    @property
    def burst(self) -> int:
        return self._burst

    def set_limits(self, rate_per_min: int, burst: int) -> None:
        rate_per_min = max(1, int(rate_per_min))
        burst = max(1, int(burst))
        with self._lock:
            self._rate_per_min = rate_per_min
            self._burst = burst
            self._refill_rate = rate_per_min / 60.0
            self._buckets.clear()

    def set_clock(self, clock: Callable[[], float]) -> None:
        with self._lock:
            self._clock = clock
            self._buckets.clear()

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def _now(self) -> float:
        return self._clock()

    def _get_bucket(self, identifier: str) -> _TokenBucket:
        bucket = self._buckets.get(identifier)
        if bucket is None:
            bucket = _TokenBucket(tokens=float(self._burst), updated_at=self._now())
            self._buckets[identifier] = bucket
        return bucket

    def _refill(self, bucket: _TokenBucket) -> None:
        now = self._now()
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._refill_rate)
        bucket.updated_at = now

    def acquire(self, identifier: str) -> RateLimitOutcome:
        with self._lock:
            bucket = self._get_bucket(identifier)
            self._refill(bucket)
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                remaining = bucket.tokens
                reset = 0.0 if bucket.tokens >= self._burst else (self._burst - bucket.tokens) / self._refill_rate
                return RateLimitOutcome(True, remaining, reset)
            reset = (1.0 - bucket.tokens) / self._refill_rate if self._refill_rate else float("inf")
            return RateLimitOutcome(False, bucket.tokens, reset)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        limiter: RateLimiter,
        should_guard: Callable[[Request], bool],
    ) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._should_guard = should_guard

    async def dispatch(self, request: Request, call_next):
        if not _guard_request(request, self._should_guard):
            return await call_next(request)

        identifier = self._identifier_for(request)
        outcome = self._limiter.acquire(identifier)
        headers = {
            "X-RateLimit-Remaining": str(max(0, int(outcome.remaining_tokens))),
            "X-RateLimit-Reset": str(max(0, int(math.ceil(outcome.reset_seconds)))),
        }
        if not outcome.allowed:
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429, headers=headers)

        response = await call_next(request)
        for name, value in headers.items():
            response.headers[name] = value
        return response

    @staticmethod
    def _identifier_for(request: Request) -> str:
        auth_header = request.headers.get("Authorization", "")
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() == "bearer" and token:
            return f"token:{token}"
        client = request.client
        if client and client[0]:
            return f"ip:{client[0]}"
        return "ip:unknown"
