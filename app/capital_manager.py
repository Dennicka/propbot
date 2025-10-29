from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping

_DEFAULT_STATE = {
    "total_capital_usdt": 0.0,
    "per_strategy_limits": {},
    "current_usage": {},
}


def _normalise_limits(payload: Mapping[str, Any]) -> dict[str, dict[str, float | None]]:
    result: dict[str, dict[str, float | None]] = {}
    for strategy, raw_entry in payload.items():
        if not isinstance(raw_entry, Mapping):
            continue
        entry: dict[str, float | None] = {}
        if "max_notional" in raw_entry:
            try:
                entry["max_notional"] = float(raw_entry["max_notional"])
            except (TypeError, ValueError):
                entry["max_notional"] = None
        result[str(strategy)] = entry
    return result


def _normalise_usage(payload: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for strategy, raw_entry in payload.items():
        if not isinstance(raw_entry, Mapping):
            continue
        value = raw_entry.get("open_notional", 0.0)
        try:
            open_notional = float(value)
        except (TypeError, ValueError):
            open_notional = 0.0
        result[str(strategy)] = {"open_notional": max(open_notional, 0.0)}
    return result


def get_capital_state_path() -> Path:
    override = os.getenv("CAPITAL_STATE_PATH")
    if override:
        return Path(override)
    return Path("data/capital_state.json")


class CapitalManager:
    """Minimal in-memory capital tracker with JSON persistence."""

    def __init__(
        self,
        *,
        state_path: str | Path | None = None,
        initial_state: Mapping[str, Any] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._path = Path(state_path) if state_path else get_capital_state_path()
        state = self._load_state()
        if state is None:
            state = self._build_initial_state(initial_state)
            self._state: dict[str, Any] = state
            self._persist()
        else:
            self._state = state

    @property
    def state_path(self) -> Path:
        return self._path

    def _build_initial_state(self, initial_state: Mapping[str, Any] | None) -> dict[str, Any]:
        if initial_state is None:
            return {**_DEFAULT_STATE}
        state = {**_DEFAULT_STATE}
        total = initial_state.get("total_capital_usdt")
        try:
            state["total_capital_usdt"] = float(total)
        except (TypeError, ValueError):
            state["total_capital_usdt"] = 0.0
        limits = initial_state.get("per_strategy_limits")
        if isinstance(limits, Mapping):
            state["per_strategy_limits"] = _normalise_limits(limits)
        usage = initial_state.get("current_usage")
        if isinstance(usage, Mapping):
            state["current_usage"] = _normalise_usage(usage)
        return state

    def _load_state(self) -> dict[str, Any] | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {**_DEFAULT_STATE}
        if not isinstance(payload, Mapping):
            return {**_DEFAULT_STATE}
        total = payload.get("total_capital_usdt")
        try:
            total_capital_usdt = float(total)
        except (TypeError, ValueError):
            total_capital_usdt = 0.0
        per_strategy_limits = _normalise_limits(payload.get("per_strategy_limits", {}))
        current_usage = _normalise_usage(payload.get("current_usage", {}))
        return {
            "total_capital_usdt": total_capital_usdt,
            "per_strategy_limits": per_strategy_limits,
            "current_usage": current_usage,
        }

    def _persist(self) -> None:
        serialisable = {
            "total_capital_usdt": float(self._state.get("total_capital_usdt", 0.0)),
            "per_strategy_limits": self._state.get("per_strategy_limits", {}),
            "current_usage": self._state.get("current_usage", {}),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            with self._path.open("w", encoding="utf-8") as handle:
                json.dump(serialisable, handle, indent=2, sort_keys=True)
        except OSError:
            pass

    def _resolve_limit(self, strategy: str) -> float | None:
        limits = self._state.get("per_strategy_limits", {})
        entry = limits.get(strategy)
        if not isinstance(entry, Mapping):
            return None
        raw_limit = entry.get("max_notional")
        if raw_limit is None:
            return None
        try:
            return float(raw_limit)
        except (TypeError, ValueError):
            return None

    def _resolve_usage(self, strategy: str) -> float:
        usage = self._state.get("current_usage", {})
        entry = usage.get(strategy)
        if not isinstance(entry, Mapping):
            return 0.0
        raw = entry.get("open_notional")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def _ensure_usage_container(self) -> dict[str, dict[str, float]]:
        raw_container = self._state.get("current_usage", {})
        if not isinstance(raw_container, Mapping):
            normalised: dict[str, dict[str, float]] = {}
        else:
            normalised = _normalise_usage(raw_container)
        self._state["current_usage"] = normalised
        return normalised

    def can_allocate(self, strategy: str, notional: float) -> bool:
        with self._lock:
            limit = self._resolve_limit(strategy)
            if limit is None:
                return True
            usage = self._resolve_usage(strategy)
            try:
                requested = float(notional)
            except (TypeError, ValueError):
                return False
            return usage + max(requested, 0.0) <= limit

    def register_fill(self, strategy: str, notional: float) -> None:
        with self._lock:
            try:
                delta = float(notional)
            except (TypeError, ValueError):
                return
            if delta <= 0:
                return
            usage = self._ensure_usage_container()
            entry = usage.setdefault(strategy, {})
            current = self._resolve_usage(strategy)
            entry["open_notional"] = current + delta
            self._persist()

    def release(self, strategy: str, notional: float) -> None:
        with self._lock:
            try:
                delta = float(notional)
            except (TypeError, ValueError):
                return
            if delta <= 0:
                return
            usage = self._ensure_usage_container()
            entry = usage.setdefault(strategy, {})
            current = self._resolve_usage(strategy)
            entry["open_notional"] = max(current - delta, 0.0)
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            per_strategy_limits = {
                strategy: dict(values)
                for strategy, values in self._state.get("per_strategy_limits", {}).items()
            }
            current_usage = {
                strategy: dict(values)
                for strategy, values in self._state.get("current_usage", {}).items()
            }
            headroom: dict[str, dict[str, float | None]] = {}
            strategies = set(per_strategy_limits) | set(current_usage)
            for strategy in strategies:
                config = per_strategy_limits.get(strategy, {})
                usage_entry = current_usage.get(strategy, {})
                limit = config.get("max_notional")
                usage_value = usage_entry.get("open_notional", 0.0)
                try:
                    usage_numeric = float(usage_value)
                except (TypeError, ValueError):
                    usage_numeric = 0.0
                try:
                    limit_numeric = float(limit)
                except (TypeError, ValueError):
                    limit_numeric = None
                if limit_numeric is None:
                    headroom[strategy] = {"headroom_notional": None}
                else:
                    headroom[strategy] = {
                        "headroom_notional": max(limit_numeric - max(usage_numeric, 0.0), 0.0)
                    }
            return {
                "total_capital_usdt": float(self._state.get("total_capital_usdt", 0.0)),
                "per_strategy_limits": per_strategy_limits,
                "current_usage": current_usage,
                "headroom": headroom,
            }


_GLOBAL_MANAGER: CapitalManager | None = None


def get_capital_manager() -> CapitalManager:
    global _GLOBAL_MANAGER
    if _GLOBAL_MANAGER is None:
        _GLOBAL_MANAGER = CapitalManager()
    return _GLOBAL_MANAGER


def reset_capital_manager(manager: CapitalManager | None = None) -> CapitalManager:
    """Reset the global capital manager instance (primarily for tests)."""

    global _GLOBAL_MANAGER
    if manager is None:
        _GLOBAL_MANAGER = CapitalManager()
    else:
        _GLOBAL_MANAGER = manager
    return _GLOBAL_MANAGER
