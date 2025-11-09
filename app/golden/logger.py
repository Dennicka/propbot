"""Golden replay event logging and normalisation utilities."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

_DEFAULT_PATH = Path("data/golden/current_run.jsonl")
_VOLATILE_KEYS = {
    "ts",
    "timestamp",
    "created_at",
    "updated_at",
    "id",
    "request_id",
    "intent_id",
    "order_id",
    "broker_order_id",
    "event_id",
    "run_id",
    "nonce",
}


def _env_flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if not lowered:
        return default
    return lowered in {"1", "true", "yes", "on"}


def _strip_volatiles(value: Any) -> Any:
    if isinstance(value, Mapping):
        items = []
        for key, inner in value.items():
            if key in _VOLATILE_KEYS:
                continue
            items.append((key, _strip_volatiles(inner)))
        return {key: inner for key, inner in sorted(items, key=lambda item: str(item[0]))}
    if isinstance(value, list):
        return [_strip_volatiles(item) for item in value]
    return value


def normalise_record(record: Mapping[str, Any]) -> dict[str, Any]:
    event_type = str(record.get("event", "unknown"))
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    cleaned_payload = _strip_volatiles(payload)
    result: dict[str, Any] = {"event": event_type, "payload": cleaned_payload}
    extras: dict[str, Any] = {}
    for key, value in record.items():
        if key in {"event", "payload"}:
            continue
        if key in _VOLATILE_KEYS:
            continue
        extras[key] = _strip_volatiles(value)
    if extras:
        result["extra"] = {key: extras[key] for key in sorted(extras)}
    return result


def normalise_events(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalised = [normalise_record(record) for record in records]
    normalised.sort(
        key=lambda entry: (
            entry.get("event", ""),
            json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        )
    )
    return normalised


class GoldenEventLogger:
    """Write golden replay events to a JSONL file when enabled."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        path: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        clock: callable | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._clock = clock if clock is not None else time.time
        if enabled is None:
            enabled = _env_flag(self._env, "GOLDEN_REPLAY_ENABLED", False)
        self._enabled = bool(enabled)
        target = Path(path) if path is not None else _DEFAULT_PATH
        self._path = target
        self._lock = threading.RLock()
        if self._enabled:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        if not self._enabled:
            return
        record_payload: dict[str, Any]
        if isinstance(payload, Mapping):
            record_payload = dict(payload)
        elif payload is None:
            record_payload = {}
        else:
            record_payload = {"value": payload}
        record = {
            "event": str(event_type or "unknown"),
            "payload": record_payload,
            "ts": self._clock(),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")


_GLOBAL_LOGGER: GoldenEventLogger | None = None


def get_golden_logger() -> GoldenEventLogger:
    global _GLOBAL_LOGGER
    if _GLOBAL_LOGGER is None:
        _GLOBAL_LOGGER = GoldenEventLogger()
    return _GLOBAL_LOGGER


__all__ = [
    "GoldenEventLogger",
    "get_golden_logger",
    "normalise_events",
    "normalise_record",
]
