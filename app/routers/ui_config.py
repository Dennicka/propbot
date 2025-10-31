from __future__ import annotations

from pathlib import Path
import shutil
import time
from typing import Any
import yaml

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config.loader import load_yaml, validate_payload

from ..services.runtime import get_state, reset_for_tests

router = APIRouter()


class ConfigPayload(BaseModel):
    yaml_text: str


def _active_path() -> Path:
    return get_state().config.path


def _backup_path() -> Path:
    return _active_path().with_suffix(_active_path().suffix + ".bak")


def _load_from_text(text: str) -> Any:
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - defensive against parser bugs
        raise HTTPException(status_code=400, detail=f"invalid config: {exc}") from exc


@router.get("/config/validate")
def validate_active_config() -> dict[str, object]:
    path = _active_path()
    errors: list[str] = []
    try:
        payload = load_yaml(path)
    except Exception as exc:  # pragma: no cover - IO failure surfaced to UI
        errors.append(f"read error: {exc}")
        payload = None
    if payload is not None:
        errors.extend(validate_payload(payload))
    return {"ok": not errors, "errors": errors}


@router.post("/config/validate")
def validate_config(payload: ConfigPayload) -> dict[str, object]:
    data = _load_from_text(payload.yaml_text)
    errors = validate_payload(data)
    if errors:
        raise HTTPException(status_code=400, detail={"ok": False, "errors": errors})
    return {"ok": True, "errors": []}


@router.post("/config/apply")
def apply_config(payload: ConfigPayload) -> dict[str, object]:
    active = _active_path()
    backup = _backup_path()
    shutil.copyfile(active, backup)
    with active.open("w", encoding="utf-8") as handle:
        handle.write(payload.yaml_text)
    token = f"rb-{int(time.time())}"
    # Reload runtime for test determinism
    reset_for_tests()
    return {"ok": True, "rollback_token": token}


class RollbackIn(BaseModel):
    token: str


@router.post("/config/rollback")
def rollback_config(body: RollbackIn) -> dict[str, object]:
    active = _active_path()
    backup = _backup_path()
    if backup.exists():
        shutil.copyfile(backup, active)
        reset_for_tests()
        return {"ok": True, "msg": "rolled back"}
    raise HTTPException(status_code=404, detail="no backup")
