from __future__ import annotations

"""Helpers exposing a normalised broker watchdog state."""

from dataclasses import dataclass
from typing import Dict, Mapping

from .broker_watchdog import (
    STATE_DEGRADED as _RAW_DEGRADED,
    STATE_DOWN as _RAW_DOWN,
    STATE_OK as _RAW_OK,
    get_broker_watchdog,
)

STATE_UP = "UP"
STATE_DEGRADED = "DEGRADED"
STATE_DOWN = "DOWN"

_STATE_ORDER = {STATE_DOWN: 0, STATE_DEGRADED: 1, STATE_UP: 2}
_STATE_MAP = {
    _RAW_OK: STATE_UP,
    _RAW_DEGRADED: STATE_DEGRADED,
    _RAW_DOWN: STATE_DOWN,
}


@dataclass(frozen=True)
class BrokerVenueState:
    venue: str
    state: str
    reason: str | None = None

    def as_dict(self) -> Dict[str, object | None]:
        return {"venue": self.venue, "state": self.state, "reason": self.reason}


@dataclass(frozen=True)
class BrokerStateSnapshot:
    per_venue: Dict[str, BrokerVenueState]
    overall: BrokerVenueState
    last_reason: str | None

    def state_for(self, venue: str) -> BrokerVenueState:
        key = (venue or "").strip().lower()
        if not key:
            return self.overall
        return self.per_venue.get(key, BrokerVenueState(venue=key, state=STATE_UP, reason=None))

    def as_dict(self) -> Dict[str, object]:
        return {
            "overall": self.overall.as_dict(),
            "last_reason": self.last_reason,
            "per_venue": {venue: entry.as_dict() for venue, entry in self.per_venue.items()},
        }


def _normalise_state(raw_state: str | None) -> str:
    state = (raw_state or "").strip().upper()
    return _STATE_MAP.get(state, STATE_DEGRADED if state else STATE_UP)


def _worst_state(current: BrokerVenueState, candidate: BrokerVenueState) -> BrokerVenueState:
    if _STATE_ORDER.get(candidate.state, 0) < _STATE_ORDER.get(current.state, 0):
        return candidate
    return current


def get_broker_state() -> BrokerStateSnapshot:
    watchdog = get_broker_watchdog()
    snapshot = watchdog.snapshot()
    per_venue_raw = snapshot.get("per_venue") if isinstance(snapshot, Mapping) else {}
    per_venue: Dict[str, BrokerVenueState] = {}
    overall = BrokerVenueState(venue="*", state=STATE_UP, reason=None)
    for venue, payload in (per_venue_raw or {}).items():
        if not isinstance(payload, Mapping):
            continue
        state = _normalise_state(str(payload.get("state")))
        reason = payload.get("last_reason") or payload.get("reason")
        entry = BrokerVenueState(
            venue=str(venue), state=state, reason=str(reason) if reason else None
        )
        per_venue[str(venue).lower()] = entry
        overall = _worst_state(overall, entry)
    last_reason = snapshot.get("last_reason") if isinstance(snapshot, Mapping) else None
    return BrokerStateSnapshot(
        per_venue=per_venue, overall=overall, last_reason=str(last_reason) if last_reason else None
    )


__all__ = [
    "BrokerVenueState",
    "BrokerStateSnapshot",
    "STATE_UP",
    "STATE_DEGRADED",
    "STATE_DOWN",
    "get_broker_state",
]
