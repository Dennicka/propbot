"""Helpers for serving static assets with HTTP caching headers."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from email.utils import format_datetime, formatdate, parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Tuple

from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

__all__ = ["CachedStaticFiles"]


def _http_datetime(timestamp: float) -> str:
    return formatdate(timestamp, usegmt=True)


def _extract_header(scope: dict, name: bytes) -> str | None:
    headers: Iterable[Tuple[bytes, bytes]] = scope.get("headers", [])
    for key, value in headers:
        if key == name:
            try:
                return value.decode("latin1")
            except UnicodeDecodeError:
                return value.decode("utf-8", errors="ignore")
    return None


def _etag_for(stat_result) -> str:
    payload = f"{stat_result.st_mtime_ns}:{stat_result.st_size}".encode("utf-8")
    digest = hashlib.sha1(payload, usedforsecurity=False).hexdigest()
    return f'W/"{digest}"'


def _not_modified(scope: dict, etag: str, mtime: float) -> bool:
    if_none_match = _extract_header(scope, b"if-none-match")
    if if_none_match:
        tags = [tag.strip() for tag in if_none_match.split(",") if tag.strip()]
        if "*" in tags or etag in tags:
            return True
    if_modified_since = _extract_header(scope, b"if-modified-since")
    if if_modified_since:
        try:
            parsed = parsedate_to_datetime(if_modified_since)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            resource_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
            if resource_dt <= parsed:
                return True
    return False


class CachedStaticFiles(StaticFiles):
    """Static file handler that injects caching headers and 304 responses."""

    def __init__(self, *, directory: str | Path, html: bool = False) -> None:
        super().__init__(directory=directory, html=html)

    async def get_response(self, path: str, scope):
        _full_path, stat_result = self.lookup_path(path)
        if stat_result is None:
            return await super().get_response(path, scope)
        etag = _etag_for(stat_result)
        last_modified = _http_datetime(stat_result.st_mtime)
        if _not_modified(scope, etag, stat_result.st_mtime):
            headers = {
                "ETag": etag,
                "Last-Modified": last_modified,
                "Cache-Control": "public, max-age=31536000, immutable",
            }
            return Response(status_code=304, headers=headers)
        response = await super().get_response(path, scope)
        if response.status_code < 400:
            response.headers.setdefault("ETag", etag)
            response.headers.setdefault("Last-Modified", last_modified)
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        return response
