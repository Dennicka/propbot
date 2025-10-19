from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError
import yaml, os, shutil, time

router = APIRouter()

CONFIG_ACTIVE = "configs/config.paper.yaml"
CONFIG_BACKUP = "configs/config.paper.yaml.bak"

class ConfigPayload(BaseModel):
    yaml_text: str

@router.post("/config/validate")
def validate_config(payload: ConfigPayload) -> dict:
    try:
        data = yaml.safe_load(payload.yaml_text)
        if not isinstance(data, dict):
            raise ValueError("root must be a mapping")
        # минимальная проверка обязательных полей
        if "profile" not in data:
            raise ValueError("missing 'profile'")
        return {"ok": True, "msg": "valid"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid config: {e}")

@router.post("/config/apply")
def apply_config(payload: ConfigPayload) -> dict:
    # backup
    shutil.copyfile(CONFIG_ACTIVE, CONFIG_BACKUP)
    # write new
    with open(CONFIG_ACTIVE, "w", encoding="utf-8") as f:
        f.write(payload.yaml_text)
    token = f"rb-{int(time.time())}"
    return {"ok": True, "rollback_token": token}

class RollbackIn(BaseModel):
    token: str

@router.post("/config/rollback")
def rollback_config(body: RollbackIn) -> dict:
    if os.path.exists(CONFIG_BACKUP):
        shutil.copyfile(CONFIG_BACKUP, CONFIG_ACTIVE)
        return {"ok": True, "msg": "rolled back"}
    raise HTTPException(status_code=404, detail="no backup")
