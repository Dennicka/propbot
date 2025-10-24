from __future__ import annotations
from pathlib import Path
import shutil
import time
import yaml

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.config import AppConfig
from ..services.runtime import get_state, reset_for_tests

router = APIRouter()


class ConfigPayload(BaseModel):
    yaml_text: str


def _active_path() -> Path:
    return get_state().config.path


def _backup_path() -> Path:
    return _active_path().with_suffix(_active_path().suffix + ".bak")


@router.post("/config/validate")
def validate_config(payload: ConfigPayload) -> dict:
    try:
        data = yaml.safe_load(payload.yaml_text)
        if not isinstance(data, dict):
            raise ValueError("root must be a mapping")
        AppConfig.model_validate(data)
        return {"ok": True, "msg": "valid"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid config: {exc}")


@router.post("/config/apply")
def apply_config(payload: ConfigPayload) -> dict:
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
def rollback_config(body: RollbackIn) -> dict:
    active = _active_path()
    backup = _backup_path()
    if backup.exists():
        shutil.copyfile(backup, active)
        reset_for_tests()
        return {"ok": True, "msg": "rolled back"}
    raise HTTPException(status_code=404, detail="no backup")
