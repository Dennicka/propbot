from __future__ import annotations

import functools
import inspect
import os
from collections.abc import Awaitable, Callable, Hashable, Iterable
from typing import Any, Protocol

from fastapi import Request

from ..services.cache import get_or_set


class CacheLoader(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


CacheVary = Callable[[Request, tuple[Any, ...], dict[str, Any]], Hashable | Iterable[Hashable] | None]


def _is_testing() -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST") or os.getenv("CI_TESTING") == "1")


def _extract_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request | None:
    for value in args:
        if isinstance(value, Request):
            return value
    for value in kwargs.values():
        if isinstance(value, Request):
            return value
    return None


async def _call_loader(fn: CacheLoader, *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result  # type: ignore[return-value]
    return result  # type: ignore[return-value]


def cache_response(
    ttl_s: float,
    *,
    vary: CacheVary | None = None,
) -> Callable[[CacheLoader], Callable[..., Awaitable[Any]]]:
    """Cache FastAPI handler responses with a per-request TTL."""

    def decorator(fn: CacheLoader) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> Any:
            request = _extract_request(tuple(args), dict(kwargs))
            if request is None or _is_testing():
                return await _call_loader(fn, *args, **kwargs)

            key_parts: list[Hashable] = [
                str(request.url.path),
                request.method.upper(),
                tuple(sorted(request.query_params.multi_items())),
            ]

            if vary is not None:
                extra = vary(request, tuple(args), dict(kwargs))
                if extra is None:
                    pass
                elif isinstance(extra, Iterable) and not isinstance(extra, (str, bytes)):
                    key_parts.extend(extra)  # type: ignore[arg-type]
                else:
                    key_parts.append(extra)

            cache_key = tuple(key_parts)

            async def _loader() -> Any:
                return await _call_loader(fn, *args, **kwargs)

            return await get_or_set(cache_key, ttl_s, _loader)

        return inner

    return decorator


__all__ = ["cache_response"]
