from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

_COMMENT_PREFIXES: tuple[str, ...] = ("#", "//")


def _strip_quotes(value: str) -> str:
    if not value:
        return value
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def load_env_file(path: str | Path = ".env") -> None:
    """Populate ``os.environ`` using key=value pairs from ``path``.

    Existing environment variables are never overwritten.
    Lines starting with ``#`` or ``//`` (after stripping leading whitespace)
    are ignored, as are empty lines. ``export `` prefixes are also supported.
    """

    env_path = Path(path)
    if not env_path.exists() or not env_path.is_file():
        return

    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(line.startswith(prefix) for prefix in _COMMENT_PREFIXES):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_quotes(value.strip())
        if not key:
            continue
        os.environ.setdefault(key, value)


def ensure_defaults(pairs: Iterable[tuple[str, str]]) -> None:
    """Set default values for missing environment variables."""

    for key, value in pairs:
        os.environ.setdefault(key, value)
