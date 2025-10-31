"""Helpers for evaluating configuration health."""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

from ..config.loader import load_yaml, validate_payload
from .runtime import get_state


def _active_config_path() -> Path:
    state = get_state()
    cfg = getattr(state, "config", None)
    path = getattr(cfg, "path", None)
    if isinstance(path, Path):
        return path
    if path:
        return Path(str(path))
    return Path("configs/config.paper.yaml")


def validate_active_config() -> Tuple[bool, list[str]]:
    """Return a tuple ``(ok, errors)`` for the active runtime config."""

    path = _active_config_path()
    errors: list[str] = []
    try:
        payload = load_yaml(path)
    except Exception as exc:  # pragma: no cover - surfaced to callers
        errors.append(f"read error: {exc}")
        payload = None
    if payload is not None:
        errors.extend(validate_payload(payload))
    return (not errors, errors)


__all__ = ["validate_active_config"]
