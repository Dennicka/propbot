"""Runtime snapshot exporter for forensic audits."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Tuple
from uuid import uuid4

from app.runtime_state_store import load_runtime_payload
from app.utils import redact_sensitive_data
from positions_store import list_records as list_position_records
from services import execution_stats_store
from app.services import approvals_store
from services.daily_reporter import load_latest_report


__all__ = ["build_snapshot_payload", "create_snapshot"]


_SNAPSHOT_DIR_ENV = "SNAPSHOT_DIR"
_DEFAULT_SNAPSHOT_DIR = Path("data/snapshots")

_RECON_ALERTS_ENV = "RECONCILIATION_ALERTS_PATH"
_DEFAULT_RECON_ALERTS_PATH = Path("data/reconciliation_alerts.json")

_EXECUTION_STATS_LIMIT = 250


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp() -> str:
    stamp = _now().isoformat()
    if stamp.endswith("+00:00"):
        stamp = stamp[:-6] + "Z"
    return stamp


def _snapshot_dir() -> Path:
    override = os.getenv(_SNAPSHOT_DIR_ENV)
    if override:
        return Path(override)
    return _DEFAULT_SNAPSHOT_DIR


def _alerts_path() -> Path:
    override = os.getenv(_RECON_ALERTS_ENV)
    if override:
        return Path(override)
    return _DEFAULT_RECON_ALERTS_PATH


def _load_reconciliation_alerts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    entries: list[dict[str, Any]] = []
    for entry in payload:
        if isinstance(entry, Mapping):
            entries.append({str(key): value for key, value in entry.items()})
    return entries


def _safe_filename(timestamp: str) -> str:
    safe = timestamp.replace(":", "-").replace("/", "-")
    safe = safe.replace("+", "-")
    return f"{safe}.json"


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _write_snapshot(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_directory(path.parent)
    serialisable = json.dumps(payload, indent=2, sort_keys=True)
    tmp_name = f".{path.name}.{uuid4().hex}.tmp"
    tmp_path = path.parent / tmp_name
    tmp_path.write_text(serialisable, encoding="utf-8")
    os.replace(tmp_path, path)


def build_snapshot_payload() -> dict[str, Any]:
    """Collect the latest runtime/positions/approvals state for export."""

    timestamp = _timestamp()
    runtime_state = load_runtime_payload()
    positions = list_position_records()
    approvals = approvals_store.list_requests()
    execution_stats = execution_stats_store.list_recent(limit=_EXECUTION_STATS_LIMIT)
    reconciliation_alerts = _load_reconciliation_alerts(_alerts_path())
    latest_report = load_latest_report() or {}

    snapshot = {
        "generated_at": timestamp,
        "runtime_state": runtime_state,
        "positions": positions,
        "approvals": approvals,
        "execution_stats": execution_stats,
        "reconciliation_alerts": reconciliation_alerts,
        "daily_report": latest_report,
    }
    return redact_sensitive_data(snapshot)


def create_snapshot(*, directory: Path | None = None) -> Tuple[dict[str, Any], Path]:
    """Persist a redacted forensic snapshot and return its payload and path."""

    payload = build_snapshot_payload()
    target_dir = directory or _snapshot_dir()
    filename = _safe_filename(str(payload.get("generated_at") or _timestamp()))
    target = target_dir / filename

    counter = 1
    stem = target.stem
    while target.exists():
        target = target_dir / f"{stem}-{counter:02d}.json"
        counter += 1

    try:
        relative_path = target.relative_to(Path.cwd())
        snapshot_path = str(relative_path)
    except ValueError:
        snapshot_path = str(target)

    payload_with_meta = dict(payload)
    payload_with_meta.setdefault("metadata", {})
    metadata = dict(payload_with_meta["metadata"])
    metadata["snapshot_path"] = snapshot_path
    payload_with_meta["metadata"] = metadata

    _write_snapshot(target, payload_with_meta)
    return payload_with_meta, target
