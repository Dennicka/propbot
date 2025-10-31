from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .schema import AppConfig, LoadedConfig, StatusThresholds


def load_yaml(path: Path | str) -> dict[str, Any]:
    cfg_path = Path(path)
    with cfg_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise TypeError(f"Configuration root must be a mapping, got {type(payload)!r}")
    return payload


def _load_thresholds(base_path: Path, app_config: AppConfig) -> StatusThresholds | None:
    reference = app_config.status_thresholds_file
    if not reference:
        return None
    thresholds_path = (base_path.parent / reference).resolve()
    payload = load_yaml(thresholds_path)
    return StatusThresholds.model_validate(payload)


def load_app_config(path: str | Path) -> LoadedConfig:
    cfg_path = Path(path)
    raw = load_yaml(cfg_path)
    app_config = AppConfig.model_validate(raw)
    thresholds = _load_thresholds(cfg_path, app_config)
    return LoadedConfig(path=cfg_path, data=app_config, thresholds=thresholds)


def validate_payload(payload: Any) -> list[str]:
    """Return a list of validation errors for ``payload``.

    The function returns an empty list when the payload is valid.
    """

    errors: list[str] = []
    try:
        AppConfig.model_validate(payload)
    except ValidationError as exc:
        for entry in exc.errors():
            location = ".".join(str(part) for part in entry.get("loc", ()))
            message = str(entry.get("msg") or "invalid")
            if location:
                errors.append(f"{location}: {message}")
            else:
                errors.append(message)
    except Exception as exc:
        errors.append(str(exc))
    return errors


__all__ = ["LoadedConfig", "load_app_config", "load_yaml", "validate_payload"]
