"""Utilities for golden-master acceptance scenarios."""

from __future__ import annotations

import asyncio
import json
from dataclasses import is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import secrets
import time

import pytest

from app import audit_log as audit_log_module
from app import ledger
from app.persistence import order_store
from app.services import autopilot as autopilot_service
from app.services import runtime as runtime_service
from app.services import safe_mode as safe_mode_service
from app.services.derivatives import DerivativesRuntime
from app.services.risk_limits import (
    RiskCheckResult,
    check_daily_loss,
    check_global_notional,
    check_symbol_notional,
)
from app.services.trading_profile import get_trading_profile, reset_trading_profile_cache

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    yaml = None


DEFAULT_TIMESTAMP = 1_700_000_000.0


def load_golden_fixture(path: Path) -> dict[str, Any]:
    """Load a golden scenario from JSON or YAML."""

    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if suffix in {".yaml", ".yml"}:
        if yaml is None:  # pragma: no cover - defensive
            raise RuntimeError("PyYAML is required to load YAML golden fixtures")
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    raise ValueError(f"unsupported golden fixture format: {path}")


def _normalise_decimal(value: Decimal | float | str | int | None) -> str:
    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return format(value, "f")
    return format(Decimal(str(value)), "f")


def _copy_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in payload}


def _apply_mapping(target: Any, payload: Mapping[str, Any]) -> None:
    for key, value in payload.items():
        if not hasattr(target, key):
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, Mapping):
            _apply_mapping(current, value)
            continue
        if isinstance(current, dict) and isinstance(value, Mapping):
            current.clear()
            for sub_key, sub_value in value.items():
                current[sub_key] = sub_value
            continue
        if isinstance(current, list) and isinstance(value, Sequence):
            current.clear()
            current.extend(value)
            continue
        setattr(target, key, value)


class _FreezeRegistry:
    def __init__(self, frozen: Iterable[Mapping[str, Any]] | None) -> None:
        entries = []
        if frozen:
            for entry in frozen:
                strategy = str(entry.get("strategy") or "").lower()
                venue = str(entry.get("venue") or "").lower()
                symbol = str(entry.get("symbol") or "").upper()
                entries.append((strategy, venue, symbol))
        self._entries = set(entries)

    def is_frozen(self, *, strategy: str | None, venue: str | None, symbol: str | None) -> bool:
        key = (str(strategy or "").lower(), str(venue or "").lower(), str(symbol or "").upper())
        return key in self._entries


class _DummyBroker:
    def __init__(self, runner: "GoldenScenarioRunner", response: Mapping[str, Any] | None) -> None:
        self._runner = runner
        self._response = dict(response or {})
        self.supports_reduce_only = True

    async def create_order(
        self,
        *,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: float | None,
        type: str,
        tif: str | None,
        strategy: str | None,
        idemp_key: str,
        reduce_only: bool,
    ) -> Mapping[str, Any]:
        self._runner.broker_calls.append(
            {
                "venue": venue,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "type": type,
                "tif": tif,
                "strategy": strategy,
                "idemp_key": idemp_key,
                "reduce_only": reduce_only,
            }
        )
        payload = dict(self._response)
        payload.setdefault("broker_order_id", "BRK-ORDER-1")
        return payload


class GoldenScenarioRunner:
    """Bootstrap runtime state and execute a golden scenario."""

    def __init__(self, scenario: Mapping[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        self.scenario = scenario
        self.monkeypatch = monkeypatch
        self.ledger_events: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        self.audit_log: list[dict[str, Any]] = []
        self.resume_calls: int = 0
        self.broker_calls: list[dict[str, Any]] = []
        self._strategy_status: Mapping[str, Any] = scenario.get("strategy_status", {})

    def apply_common_patches(self) -> None:
        timestamp = float(self.scenario.get("time", DEFAULT_TIMESTAMP))

        def _fixed_time() -> float:
            return timestamp

        self.monkeypatch.setattr(time, "time", _fixed_time)
        self.monkeypatch.setattr(time, "perf_counter", _fixed_time)

        entropy = str(self.scenario.get("entropy", "feedfeedfeedfeedfeed"))

        def _token_hex(length: int = 10) -> str:
            repeated = (entropy * ((length * 2 + len(entropy) - 1) // len(entropy)))[: length * 2]
            return repeated

        self.monkeypatch.setattr(secrets, "token_hex", _token_hex)

        env_payload = self.scenario.get("env", {})
        for key, value in env_payload.items():
            self.monkeypatch.setenv(str(key), str(value))

        profile = self.scenario.get("profile")
        if profile:
            self.monkeypatch.setenv("TRADING_PROFILE", str(profile))
            self.monkeypatch.setenv("PROFILE", str(profile))
        self.monkeypatch.setenv("ORDERS_DB_URL", "sqlite:///:memory:")
        self.monkeypatch.setenv("REQUEST_ID_PREFIX", self.scenario.get("request_id_prefix", "rid"))

        self.monkeypatch.setattr(runtime_service, "_persist_runtime_payload", lambda updates: None)
        self.monkeypatch.setattr(
            runtime_service, "_persist_control_snapshot", lambda snapshot: None
        )
        self.monkeypatch.setattr(runtime_service, "_persist_safety_snapshot", lambda snapshot: None)
        self.monkeypatch.setattr(
            runtime_service, "_persist_autopilot_snapshot", lambda snapshot: None
        )

        reset_trading_profile_cache()

        positions = [dict(entry) for entry in self.scenario.get("positions", [])]
        balances = [dict(entry) for entry in self.scenario.get("balances", [])]

        self.monkeypatch.setattr(
            ledger, "fetch_positions", lambda: [dict(entry) for entry in positions]
        )
        self.monkeypatch.setattr(
            ledger, "fetch_balances", lambda: [dict(entry) for entry in balances]
        )

        def _record_event(*, level: str, code: str, payload: Mapping[str, Any]) -> None:
            self.ledger_events.append(
                {"level": level, "code": code, "payload": _copy_mapping(dict(payload))}
            )

        self.monkeypatch.setattr(ledger, "record_event", _record_event)

        def _audit_stub(
            operator_name: str, role: str, action: str, details: Any | None = None
        ) -> None:
            payload = {
                "operator": operator_name,
                "role": role,
                "action": action,
                "details": details,
            }
            self.audit_log.append(payload)

        self.monkeypatch.setattr(audit_log_module, "log_operator_action", _audit_stub)

        freeze_payload = self.scenario.get("freeze")
        self.monkeypatch.setattr(
            "app.risk.freeze.get_freeze_registry", lambda: _FreezeRegistry(freeze_payload)
        )

        daily_loss = self.scenario.get("daily_loss_state", {})
        self.monkeypatch.setattr(
            "app.risk.daily_loss.get_daily_loss_cap_state",
            lambda: dict(daily_loss),
        )

        def _emit_alert(kind: str, text: str, extra: Mapping[str, Any] | None = None) -> None:
            self.alerts.append({"kind": kind, "text": text, "extra": dict(extra or {})})

        self.monkeypatch.setattr(autopilot_service, "emit_alert", _emit_alert)

        async def _resume_loop_stub() -> object:
            self.resume_calls += 1
            return runtime_service.get_loop_state()

        self.monkeypatch.setattr(autopilot_service, "resume_loop", _resume_loop_stub)
        self.monkeypatch.setattr(
            autopilot_service,
            "build_strategy_status",
            lambda: {key: dict(value) for key, value in self._strategy_status.items()},
        )

        from app.router import order_router as order_router_module
        from app.risk import exposure_caps as exposure_caps_module

        class _PretradeValidatorStub:
            def validate(
                self, payload: Mapping[str, Any]
            ) -> tuple[bool, str | None, Mapping[str, Any] | None]:
                return True, None, None

        self.monkeypatch.setattr(
            order_router_module,
            "get_pretrade_validator",
            lambda: _PretradeValidatorStub(),
        )
        self.monkeypatch.setattr(
            exposure_caps_module,
            "resolve_caps",
            lambda cfg, symbol, side, venue: {
                "global_max_abs": None,
                "side_max_abs": None,
                "venue_max_abs": None,
            },
        )

    def bootstrap_runtime(self) -> None:
        def _bootstrap_derivatives(config, safe_mode: bool = True) -> DerivativesRuntime:
            runtime = DerivativesRuntime(config=config)
            runtime.venues = {}
            return runtime

        self.monkeypatch.setattr(runtime_service, "bootstrap_derivatives", _bootstrap_derivatives)
        runtime_service.reset_for_tests()
        safe_mode_service.reset_safe_mode_for_tests()

        runtime_overrides = self.scenario.get("runtime", {})
        state = runtime_service.get_state()
        control_override = runtime_overrides.get("control")
        if isinstance(control_override, Mapping):
            _apply_mapping(state.control, control_override)
        autopilot_override = runtime_overrides.get("autopilot")
        if isinstance(autopilot_override, Mapping):
            _apply_mapping(state.autopilot, autopilot_override)
        safety_override = runtime_overrides.get("safety")
        if isinstance(safety_override, Mapping):
            if "limits" in safety_override and isinstance(safety_override["limits"], Mapping):
                _apply_mapping(state.safety.limits, safety_override["limits"])
            if "counters" in safety_override and isinstance(safety_override["counters"], Mapping):
                _apply_mapping(state.safety.counters, safety_override["counters"])
            general = {
                key: value
                for key, value in safety_override.items()
                if key not in {"limits", "counters"}
            }
            _apply_mapping(state.safety, general)
        guards_override = runtime_overrides.get("guards")
        if isinstance(guards_override, Mapping):
            for name, payload in guards_override.items():
                if not isinstance(payload, Mapping):
                    continue
                guard = state.guards.get(name)
                if guard is None:
                    continue
                _apply_mapping(guard, payload)
        risk_override = runtime_overrides.get("risk")
        if isinstance(risk_override, Mapping):
            if "current" in risk_override and isinstance(risk_override["current"], Mapping):
                _apply_mapping(state.risk.current, risk_override["current"])
            if "breaches" in risk_override and isinstance(risk_override["breaches"], Sequence):
                state.risk.breaches = [
                    runtime_service.RiskBreach(**{str(k): v for k, v in entry.items()})
                    for entry in risk_override["breaches"]
                    if isinstance(entry, Mapping)
                ]
        auto_hedge_override = runtime_overrides.get("auto_hedge")
        if isinstance(auto_hedge_override, Mapping):
            _apply_mapping(state.auto_hedge, auto_hedge_override)

        safe_mode_payload = self.scenario.get("safe_mode")
        if isinstance(safe_mode_payload, Mapping):
            state_value = str(safe_mode_payload.get("state", "NORMAL")).upper()
            reason = safe_mode_payload.get("reason", "scenario")
            extra = {
                key: value
                for key, value in safe_mode_payload.items()
                if key not in {"state", "reason"}
            }
            if state_value == "HOLD":
                safe_mode_service.enter_hold(str(reason), extra=extra or None)
            elif state_value == "KILL":
                safe_mode_service.enter_kill(str(reason), extra=extra or None)
            else:
                safe_mode_service.reset_safe_mode_for_tests()

    def run_autopilot(self) -> dict[str, Any]:
        async def _evaluate() -> None:
            await autopilot_service.evaluate_startup()

        asyncio.run(_evaluate())
        state = runtime_service.get_state()
        safety_state = state.safety
        autopilot_state = state.autopilot
        control_state = state.control
        safe_status = safe_mode_service.get_safe_mode_state()
        return {
            "autopilot": {
                "last_decision": autopilot_state.last_decision,
                "last_decision_reason": autopilot_state.last_decision_reason,
                "armed": autopilot_state.armed,
                "target_mode": autopilot_state.target_mode,
                "target_safe_mode": autopilot_state.target_safe_mode,
            },
            "control": {
                "mode": control_state.mode,
                "safe_mode": control_state.safe_mode,
                "auto_loop": control_state.auto_loop,
                "preflight_passed": control_state.preflight_passed,
            },
            "safety": {
                "hold_active": safety_state.hold_active,
                "hold_reason": safety_state.hold_reason,
                "last_pretrade_block": dict(safety_state.last_pretrade_block or {}),
            },
            "safe_mode": {
                "state": safe_status.state.value,
                "reason": safe_status.reason,
            },
            "resume_calls": self.resume_calls,
            "alerts": list(self.alerts),
            "ledger_events": list(self.ledger_events),
        }

    def run_order_router(self) -> dict[str, Any]:
        from app.router import order_router as order_router_module

        request = self.scenario.get("order", {}).get("request", {})
        broker_response = self.scenario.get("order", {}).get("broker_response", {})
        broker = _DummyBroker(self, broker_response)
        router = order_router_module.OrderRouter(broker)

        async def _submit() -> order_router_module.OrderRef:
            return await router.submit_order(**request)

        error: dict[str, Any] | None = None
        ref: order_router_module.OrderRef | None = None
        try:
            ref = asyncio.run(_submit())
        except order_router_module.PretradeGateThrottled as exc:
            error = {
                "type": "PretradeGateThrottled",
                "reason": exc.reason,
                "details": dict(exc.details),
            }

        intent_snapshot: dict[str, Any] | None = None
        if ref is not None:
            with order_store.session_scope() as session:
                intent = order_store.load_intent(session, ref.intent_id)
                if intent is not None:
                    intent_snapshot = {
                        "intent_id": intent.intent_id,
                        "request_id": intent.request_id,
                        "account": intent.account,
                        "venue": intent.venue,
                        "symbol": intent.symbol,
                        "side": intent.side,
                        "type": intent.type,
                        "tif": intent.tif,
                        "qty": intent.qty,
                        "price": intent.price,
                        "strategy": intent.strategy,
                        "state": intent.state.value,
                        "broker_order_id": intent.broker_order_id,
                        "remaining_qty": intent.remaining_qty,
                    }

        state = runtime_service.get_state()
        safety_state = state.safety
        safe_status = safe_mode_service.get_safe_mode_state()
        return {
            "order_ref": (
                {
                    "intent_id": getattr(ref, "intent_id", None) if ref else None,
                    "request_id": getattr(ref, "request_id", None) if ref else None,
                    "state": ref.state.value if ref else None,
                    "broker_order_id": getattr(ref, "broker_order_id", None) if ref else None,
                }
                if ref
                else None
            ),
            "order_intent": intent_snapshot,
            "broker_calls": list(self.broker_calls),
            "error": error,
            "ledger_events": list(self.ledger_events),
            "audit_log": list(self.audit_log),
            "safe_mode": {"state": safe_status.state.value, "reason": safe_status.reason},
            "last_pretrade_block": dict(safety_state.last_pretrade_block or {}),
        }

    def run_risk_limits(self) -> dict[str, Any]:
        profile = get_trading_profile()
        checks = self.scenario.get("risk_checks", {})
        result: dict[str, Any] = {}
        symbol_payload = checks.get("symbol_notional")
        if isinstance(symbol_payload, Mapping):
            outcome = check_symbol_notional(
                str(symbol_payload.get("symbol")), symbol_payload.get("projected"), profile
            )
            result["symbol_notional"] = _serialise_risk_result(outcome)
        global_payload = checks.get("global_notional")
        if isinstance(global_payload, Mapping):
            outcome = check_global_notional(global_payload.get("projected"), profile)
            result["global_notional"] = _serialise_risk_result(outcome)
        daily_payload = checks.get("daily_loss")
        if isinstance(daily_payload, Mapping):
            outcome = check_daily_loss(daily_payload.get("current_loss"), profile)
            result["daily_loss"] = _serialise_risk_result(outcome)
        return result


def _serialise_risk_result(result: RiskCheckResult) -> dict[str, Any]:
    return {
        "allowed": result.allowed,
        "limit": _normalise_decimal(result.limit),
        "projected": _normalise_decimal(result.projected),
    }


def assert_expected(expected: Any, actual: Any) -> None:
    if isinstance(expected, Mapping):
        assert isinstance(actual, Mapping), f"expected mapping got {type(actual)!r}"
        for key, value in expected.items():
            assert key in actual, f"missing key: {key}"
            assert_expected(value, actual[key])
        return
    if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes)):
        assert isinstance(actual, Sequence), f"expected sequence got {type(actual)!r}"
        assert len(actual) >= len(expected)
        for index, item in enumerate(expected):
            assert_expected(item, actual[index])
        return
    if isinstance(expected, float):
        assert actual == pytest.approx(expected)
    else:
        assert actual == expected
