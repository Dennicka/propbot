"""Persistent storage for critical action approval requests."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableSequence

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data_dir() -> Path:
    override = os.getenv("OPS_ALERTS_DIR")
    if override:
        path = Path(override)
    else:
        path = Path(__file__).resolve().parents[2] / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _approvals_path() -> Path:
    override = os.getenv("OPS_APPROVALS_FILE")
    if override:
        path = Path(override)
    else:
        path = _data_dir() / "ops_approvals.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_records(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError:
        return []
    except OSError:
        return []
    if not isinstance(payload, list):
        return []
    records: List[Dict[str, Any]] = []
    for entry in payload:
        if isinstance(entry, Mapping):
            record = dict(entry)
            record.setdefault("status", "pending")
            record.setdefault("parameters", {})
            records.append(record)
    return records


def _write_records(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    snapshot = [dict(entry) for entry in records]
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, sort_keys=True)
    except OSError:
        pass


def _find_record(records: MutableSequence[Dict[str, Any]], request_id: str) -> Dict[str, Any] | None:
    for record in records:
        if str(record.get("id")) == str(request_id):
            return record
    return None


def create_request(
    action: str,
    *,
    requested_by: str | None = None,
    parameters: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    path = _approvals_path()
    record = {
        "id": uuid.uuid4().hex,
        "action": str(action),
        "status": "pending",
        "requested_by": requested_by,
        "requested_ts": _now(),
        "parameters": dict(parameters or {}),
    }
    with _LOCK:
        records = _load_records(path)
        records.append(record)
        _write_records(path, records)
    return dict(record)


def approve_request(request_id: str, *, actor: str | None = None) -> Dict[str, Any]:
    path = _approvals_path()
    with _LOCK:
        records = _load_records(path)
        record = _find_record(records, request_id)
        if record is None:
            raise KeyError("request_not_found")
        if str(record.get("status")) != "pending":
            raise ValueError("request_not_pending")
        record["status"] = "approved"
        record["approved_by"] = actor
        record["approved_ts"] = _now()
        _write_records(path, records)
        return dict(record)


def get_request(request_id: str) -> Dict[str, Any] | None:
    path = _approvals_path()
    with _LOCK:
        records = _load_records(path)
    record = _find_record(records, request_id)
    return dict(record) if record else None


def list_requests(*, status: str | None = None) -> List[Dict[str, Any]]:
    path = _approvals_path()
    with _LOCK:
        records = _load_records(path)
    if status is None:
        return [dict(entry) for entry in records]
    filtered: List[Dict[str, Any]] = []
    for entry in records:
        if str(entry.get("status")) == status:
            filtered.append(dict(entry))
    return filtered


def reset_for_tests() -> None:
    path = _approvals_path()
    try:
        path.unlink()
    except OSError:
        pass
