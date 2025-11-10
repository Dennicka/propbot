from __future__ import annotations

import os
import threading
import time
import logging
from collections.abc import Mapping as ABCMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Mapping, Optional, Tuple

from .runtime_state_store import load_runtime_payload
from .services.runtime import get_state
from .risk.core import _current_risk_metrics


LOGGER = logging.getLogger(__name__)


def _env_int(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _env_float(name: str) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass
class StrategyState:
    name: str
    cooldown_sec: int = 0
    last_result: str | None = None
    last_error: str | None = None
    last_run_ts: float | None = None
    cooldown_until: float | None = None
    status_reason: str | None = None


def check_risk_gates() -> Dict[str, object | None]:
    """Aggregate runtime safety flags and basic risk-cap checks."""

    state = get_state()
    safety = getattr(state, "safety", None)
    control = getattr(state, "control", None)
    autopilot = getattr(state, "autopilot", None)

    hold_active = bool(getattr(safety, "hold_active", False))
    safe_mode = bool(getattr(control, "safe_mode", False))
    dry_run_mode = bool(getattr(control, "dry_run_mode", False) or getattr(control, "dry_run", False))
    autopilot_enabled = bool(getattr(autopilot, "enabled", False))

    metrics = _current_risk_metrics()

    max_open_positions = _env_int("MAX_OPEN_POSITIONS")
    max_total_notional = _env_float("MAX_TOTAL_NOTIONAL_USDT")

    risk_caps_ok = True
    reason_if_blocked: str | None = None

    if hold_active or safe_mode:
        risk_caps_ok = False
        reason_if_blocked = "hold_active"
    elif max_open_positions and max_open_positions > 0 and metrics.open_positions > max_open_positions:
        risk_caps_ok = False
        reason_if_blocked = "risk_limit"
    elif max_total_notional and max_total_notional > 0 and metrics.total_notional > max_total_notional:
        risk_caps_ok = False
        reason_if_blocked = "risk_limit"

    return {
        "hold_active": hold_active,
        "safe_mode": safe_mode,
        "dry_run_mode": dry_run_mode,
        "autopilot_enabled": autopilot_enabled,
        "risk_caps_ok": risk_caps_ok,
        "reason_if_blocked": reason_if_blocked,
    }


class StrategyOrchestrator:
    """Simple in-memory registry of strategy execution state."""

    def __init__(self, strategies: Optional[Mapping[str, int]] = None) -> None:
        base_registry = strategies or {
            "hedger": 5,
            "cross_exchange_arb": 10,
            "scanner": 15,
        }
        self._lock = threading.RLock()
        self._strategies: Dict[str, StrategyState] = {
            name: StrategyState(name=name, cooldown_sec=max(0, int(cooldown)))
            for name, cooldown in base_registry.items()
        }
        self._last_alert_signature: Dict[str, Tuple[str, str, str, str]] = {}

    def register_strategy(self, name: str, *, cooldown_sec: int = 0) -> None:
        with self._lock:
            self._strategies[name] = StrategyState(
                name=name,
                cooldown_sec=max(0, int(cooldown_sec)),
            )

    def compute_next_plan(self) -> Dict[str, object]:
        with self._lock:
            risk_summary = check_risk_gates()
            now = time.time()
            timestamp = datetime.now(timezone.utc).isoformat()
            entries = []
            for name, state in self._strategies.items():
                decision = "run"
                reason = "ok"

                if not risk_summary.get("risk_caps_ok", True):
                    decision = "skip"
                    reason = str(risk_summary.get("reason_if_blocked") or "risk_blocked")
                    state.status_reason = reason
                elif state.cooldown_until and state.cooldown_until > now:
                    decision = "cooldown"
                    reason = state.status_reason or "cooldown_active"
                else:
                    state.status_reason = None
                    if state.cooldown_until and state.cooldown_until <= now:
                        state.cooldown_until = None

                entry = {
                    "name": name,
                    "decision": decision,
                    "reason": reason,
                    "status_reason": state.status_reason,
                    "last_result": state.last_result,
                    "last_error": state.last_error,
                    "last_run_ts": state.last_run_ts,
                    "cooldown_sec": state.cooldown_sec,
                }
                if state.cooldown_until and state.cooldown_until > now:
                    entry["cooldown_remaining_sec"] = max(0, int(state.cooldown_until - now))
                entries.append(entry)
            return {"strategies": entries, "ts": timestamp, "risk_gates": risk_summary}

    def record_result(self, name: str, outcome: str, error: str | None = None) -> None:
        outcome_norm = str(outcome).lower()
        now = time.time()
        with self._lock:
            state = self._strategies.get(name)
            if state is None:
                state = StrategyState(name=name, cooldown_sec=0)
                self._strategies[name] = state
            state.last_result = outcome_norm
            state.last_error = error if error is None else str(error)
            state.last_run_ts = now
            if outcome_norm != "ok":
                state.status_reason = state.last_error or outcome_norm
                if state.cooldown_sec > 0:
                    state.cooldown_until = now + state.cooldown_sec
            else:
                state.status_reason = None
                state.cooldown_until = None

    def reset(self) -> None:
        with self._lock:
            for name, state in list(self._strategies.items()):
                self._strategies[name] = StrategyState(name=name, cooldown_sec=state.cooldown_sec)
            self._last_alert_signature.clear()

    def emit_alerts_if_needed(self, notifier) -> None:
        """Emit operator alerts when orchestration decisions block trading."""

        if notifier is None:
            return

        send_alert = getattr(notifier, "alert_ops", None)
        if not callable(send_alert):
            emit_alert = getattr(notifier, "emit_alert", None)

            if not callable(emit_alert):
                return

            def _fallback_alert(*, text: str, kind: str = "ops_alert", extra: Mapping[str, object] | None = None) -> None:
                emit_alert(kind=kind, text=text, extra=extra or None)

            send_alert = _fallback_alert

        plan = self.compute_next_plan()
        risk_summary = plan.get("risk_gates") or {}
        autopilot_enabled = bool(risk_summary.get("autopilot_enabled"))
        autopilot_label = "ON" if autopilot_enabled else "OFF"

        strategies = plan.get("strategies") or []
        alerts: list[tuple[str, str, str, str, Dict[str, object]]] = []

        with self._lock:
            for entry in strategies:
                if not isinstance(entry, Mapping):
                    continue

                name = str(entry.get("name") or "").strip()
                if not name:
                    continue

                decision = str(entry.get("decision") or "").lower()
                raw_reason = str(entry.get("reason") or "").strip() or str(entry.get("status_reason") or "").strip()
                last_result = str(entry.get("last_result") or "").lower()

                reason = raw_reason or "ok"
                should_alert = False

                if decision == "skip":
                    if bool(risk_summary.get("safe_mode")):
                        reason = "safe_mode"
                    elif bool(risk_summary.get("hold_active")):
                        reason = "hold_active"
                    elif raw_reason:
                        reason = raw_reason
                    if reason in {"hold_active", "risk_limit", "safe_mode"}:
                        should_alert = True
                elif decision == "cooldown" and last_result == "fail":
                    reason = raw_reason or "cooldown"
                    should_alert = True

                signature = (decision, reason, autopilot_label, last_result)

                if should_alert:
                    if self._last_alert_signature.get(name) == signature:
                        continue
                    self._last_alert_signature[name] = signature
                    extra: Dict[str, object] = {
                        "strategy": name,
                        "decision": decision,
                        "reason": reason,
                        "autopilot": autopilot_label,
                    }
                    if last_result:
                        extra["last_result"] = last_result
                    alerts.append((name, decision, reason, autopilot_label, extra))
                else:
                    if name in self._last_alert_signature:
                        del self._last_alert_signature[name]

        for name, decision, reason, autopilot_text, extra in alerts:
            try:
                send_alert(
                    text=f"[orchestrator] strategy={name} decision={decision} "
                    f"reason={reason} autopilot={autopilot_text}",
                    kind="orchestrator_alert",
                    extra=extra,
                )
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning(
                    "orchestrator.alert_failed",
                    extra={
                        "strategy": name,
                        "decision": decision,
                        "reason": reason,
                        "error": str(exc),
                    },
                )


orchestrator = StrategyOrchestrator()
