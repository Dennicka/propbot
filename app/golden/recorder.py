"""Golden replay recording utilities."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

LOGGER = logging.getLogger(__name__)

_GOLDEN_TRACE_PATH = Path("data/golden_trace.log")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if not lowered:
        return default
    return lowered in {"1", "true", "yes", "on"}


def golden_record_enabled() -> bool:
    """Return True when golden recording is enabled via env flag."""

    return _env_flag("GOLDEN_RECORD_ENABLED", False)


def golden_replay_enabled() -> bool:
    """Return True when the golden replay harness should run."""

    return _env_flag("GOLDEN_REPLAY_ENABLED", False)


@dataclass(frozen=True)
class _DecisionEvent:
    venue: str
    symbol: str
    side: str
    size: float
    reason: str | None
    runtime_state: str
    hold: bool
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "reason": self.reason,
            "runtime_state": self.runtime_state,
            "hold": self.hold,
            "dry_run": self.dry_run,
        }
        return payload


class GoldenDecisionRecorder:
    """Append-only JSONL recorder for decision events."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._path = path or _GOLDEN_TRACE_PATH
        self._enabled = golden_record_enabled() if enabled is None else bool(enabled)
        self._lock = threading.RLock()
        if self._enabled:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> Path:
        return self._path

    def record_events(self, events: Iterable[_DecisionEvent]) -> None:
        if not self._enabled:
            return
        lines: list[str] = []
        for event in events:
            payload = event.to_dict()
            try:
                line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                LOGGER.debug(
                    "golden record skipped unserialisable payload", extra={"payload": payload}
                )
                continue
            lines.append(line)
        if not lines:
            return
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    for line in lines:
                        handle.write(line)
                        handle.write("\n")
        except OSError as exc:  # pragma: no cover - defensive logging
            LOGGER.warning(
                "golden record write failed", extra={"error": str(exc), "path": str(self._path)}
            )


_GLOBAL_RECORDER: GoldenDecisionRecorder | None = None


def get_decision_recorder() -> GoldenDecisionRecorder:
    global _GLOBAL_RECORDER
    if _GLOBAL_RECORDER is None:
        _GLOBAL_RECORDER = GoldenDecisionRecorder()
    return _GLOBAL_RECORDER


def _normalise_runtime_state(runtime_state: Mapping[str, Any] | None) -> str:
    if runtime_state is None:
        return "{}"
    try:
        return json.dumps(runtime_state, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        LOGGER.debug(
            "golden record runtime state serialisation failed", extra={"state": runtime_state}
        )
        return "{}"


def _extract_orders(orders: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    if not orders:
        return extracted
    for order in orders:
        venue = str(order.get("venue") or order.get("exchange") or "unknown")
        side = str(order.get("side") or order.get("direction") or "unknown")
        qty_raw = order.get("qty")
        if qty_raw is None:
            qty_raw = order.get("quantity")
        try:
            size = float(qty_raw)
        except (TypeError, ValueError):
            size = 0.0
        extracted.append(
            {
                "venue": venue,
                "symbol": str(order.get("symbol") or ""),
                "side": side,
                "size": size,
            }
        )
    return extracted


def record_execution(
    *,
    symbol: str,
    plan_payload: Mapping[str, Any],
    orders: Sequence[Mapping[str, Any]] | None,
    reason: str | None,
    runtime_state: Mapping[str, Any] | None,
    hold: bool,
    dry_run: bool,
) -> None:
    recorder = get_decision_recorder()
    if not recorder.enabled:
        return
    serialised_state = _normalise_runtime_state(runtime_state)
    extracted_orders = _extract_orders(orders)
    events: list[_DecisionEvent] = []
    if extracted_orders:
        for order in extracted_orders:
            venue = order.get("venue") or "unknown"
            side = order.get("side") or "unknown"
            symbol_value = order.get("symbol") or symbol
            size_value = order.get("size")
            try:
                size = float(size_value)
            except (TypeError, ValueError):
                size = 0.0
            events.append(
                _DecisionEvent(
                    venue=venue,
                    symbol=str(symbol_value or symbol),
                    side=side,
                    size=size,
                    reason=reason,
                    runtime_state=serialised_state,
                    hold=hold,
                    dry_run=dry_run,
                )
            )
    else:
        venues = plan_payload.get("venues")
        if isinstance(venues, Sequence) and not isinstance(venues, (str, bytes)) and venues:
            venue = str(venues[0])
        else:
            venue = "unknown"
        events.append(
            _DecisionEvent(
                venue=venue,
                symbol=str(plan_payload.get("symbol") or symbol),
                side="none",
                size=0.0,
                reason=reason,
                runtime_state=serialised_state,
                hold=hold,
                dry_run=dry_run,
            )
        )
    recorder.record_events(events)


__all__ = [
    "GoldenDecisionRecorder",
    "golden_record_enabled",
    "golden_replay_enabled",
    "get_decision_recorder",
    "record_execution",
]
