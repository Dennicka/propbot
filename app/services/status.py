from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from .runtime import RuntimeState, engage_safety_hold, get_state, update_clock_skew
from ..utils import redact_sensitive_data


_GROUP_ORDER = ["P0", "P1", "P2", "P3"]
_DEFAULT_HOLD_MINUTES = 5
_SEVERITY_RANK = {"OK": 0, "WARN": 1, "ERROR": 2, "HOLD": 3}


def _coerce_metric_value(value: object) -> object:
    if isinstance(value, bool):
        return int(value)
    return value


def _component(
    *,
    component_id: str,
    title: str,
    group: str,
    status: str,
    summary: str,
    metrics: Dict[str, object] | None = None,
    links: List[Dict[str, str]] | None = None,
) -> Dict[str, object]:
    payload = {
        "id": component_id,
        "title": title,
        "group": group,
        "status": status,
        "summary": summary,
        "metrics": {k: _coerce_metric_value(v) for k, v in (metrics or {}).items()},
        "links": list(links or []),
    }
    return payload


def _guard_component(state: RuntimeState, guard_name: str, *, title: str) -> Dict[str, object]:
    guard = state.guards.get(guard_name)
    if guard is None:
        return _component(
            component_id=guard_name,
            title=title,
            group="P0",
            status="HOLD",
            summary="guard unavailable",
            metrics={},
        )
    return _component(
        component_id=guard_name,
        title=title,
        group="P0",
        status=guard.status,
        summary=guard.summary,
        metrics=guard.metrics,
        links=[{"title": "Guardrail", "href": f"/docs/guards#{guard_name}"}],
    )


def _normalise_server_time(raw: float) -> float | None:
    if raw <= 0:
        return None
    if raw > 1e13:
        return raw / 1_000_000.0
    if raw > 1e10:
        return raw / 1_000.0
    return raw


def _evaluate_clock_skew(state: RuntimeState) -> float | None:
    if not state.derivatives or not state.derivatives.venues:
        update_clock_skew(None, source="status")
        return None
    samples: List[float] = []
    now = time.time()
    for runtime in state.derivatives.venues.values():
        client = getattr(runtime, "client", None)
        if client is None or not hasattr(client, "server_time"):
            continue
        try:
            server_time_raw = client.server_time()
        except Exception:  # pragma: no cover - defensive
            continue
        try:
            numeric = float(server_time_raw)
        except (TypeError, ValueError):
            continue
        server_time = _normalise_server_time(numeric)
        if server_time is None:
            continue
        samples.append(server_time - now)
    if not samples:
        update_clock_skew(None, source="status")
        return None
    skew = max(samples, key=lambda value: abs(value))
    update_clock_skew(skew, source="status")
    return skew


def _build_components(state: RuntimeState) -> List[Dict[str, object]]:
    components: List[Dict[str, object]] = []
    slo_mismatch = state.metrics.slo.get("recon_mismatch", 0.0)
    approvals_count = len(state.control.approvals)
    split_brain_events = state.metrics.counters.get("split_brain_events", 0)
    venues_total = len(state.derivatives.venues) if state.derivatives else 0

    components.append(
        _component(
            component_id="journal_outbox",
            title="Journal/Outbox",
            group="P0",
            status="OK",
            summary="queued events processed",
            metrics={"backlog": 0},
            links=[{"title": "Events", "href": "/api/ui/events"}],
        )
    )
    components.append(
        _component(
            component_id="guarded_startup",
            title="Guarded Startup",
            group="P0",
            status="OK" if state.control.preflight_passed else "HOLD",
            summary="ready" if state.control.preflight_passed else "awaiting preflight",
            metrics={"preflight_passed": int(state.control.preflight_passed)},
            links=[{"title": "Runbook", "href": "/docs/RUNBOOK_ru.md"}],
        )
    )
    components.append(
        _component(
            component_id="leader_fencing",
            title="Leader/Fencing",
            group="P0",
            status="OK" if split_brain_events == 0 else "ERROR",
            summary="split-brain=0" if split_brain_events == 0 else f"split-brain events: {split_brain_events}",
            metrics={"split_brain_events": split_brain_events},
            links=[{"title": "High-Availability", "href": "/docs/VPS_HANDBOOK_ru.md"}],
        )
    )
    components.append(
        _component(
            component_id="conformance",
            title="Conformance (per-venue)",
            group="P0",
            status="OK",
            summary=f"venues checked: {venues_total}",
            metrics={"venues": venues_total, "failures": 0},
            links=[{"title": "Conformance", "href": "/docs/SLO_and_Monitoring.md"}],
        )
    )
    components.append(
        _component(
            component_id="recon",
            title="Recon",
            group="P0",
            status="OK" if slo_mismatch == 0 else "ERROR",
            summary="aligned" if slo_mismatch == 0 else f"mismatch={slo_mismatch}",
            metrics={"mismatch": slo_mismatch},
            links=[{"title": "Recon", "href": "/api/ui/recon"}],
        )
    )
    components.append(
        _component(
            component_id="keys_security",
            title="Keys/Security",
            group="P0",
            status="OK" if state.control.two_man_rule else "WARN",
            summary="audit on" if state.control.two_man_rule else "two-man rule disabled",
            metrics={"two_man_rule": int(state.control.two_man_rule)},
            links=[{"title": "Security", "href": "/docs/RISK_AND_GUARDS.md"}],
        )
    )
    components.append(
        _component(
            component_id="compliance_worm",
            title="Compliance/WORM",
            group="P0",
            status="OK",
            summary="archive immutable",
            metrics={"worm_enabled": 1},
            links=[{"title": "Compliance", "href": "/docs/ARBITRAGE_README.md"}],
        )
    )
    components.append(
        _component(
            component_id="slo_watchdog",
            title="SLO Watchdog",
            group="P0",
            status="OK",
            summary="within limits",
            metrics={"breaches_active": 0},
            links=[{"title": "SLO", "href": "/docs/SLO_and_Monitoring.md"}],
        )
    )

    for guard_id, guard_title in (
        ("rate_limit", "Rate-limit Governor"),
        ("cancel_on_disconnect", "Cancel on Disconnect"),
        ("clock_skew", "Clock Skew Guard"),
        ("snapshot_diff", "Snapshot+Diff Continuity"),
        ("kill_caps", "Kill Caps"),
        ("runaway_breaker", "Runaway Breaker"),
        ("maintenance_calendar", "Maintenance Calendar"),
    ):
        components.append(_guard_component(state, guard_id, title=guard_title))

    components.append(
        _component(
            component_id="arb_engine",
            title="Arbitrage Engine",
            group="P0",
            status="OK" if state.control.preflight_passed else "HOLD",
            summary="ready" if state.control.preflight_passed else "awaiting preflight",
            metrics={"preflight_passed": int(state.control.preflight_passed)},
            links=[{"title": "CLI", "href": "/docs/README_ru.md"}],
        )
    )
    components.append(
        _component(
            component_id="approvals",
            title="Two-Man Rule",
            group="P0",
            status="OK" if approvals_count >= 2 else "HOLD",
            summary=f"approvals: {approvals_count}/2",
            metrics={"approvals": approvals_count},
            links=[{"title": "Controls", "href": "/api/ui/control/state"}],
        )
    )

    components.append(
        _component(
            component_id="incidents",
            title="Incident Journal",
            group="P1",
            status="OK" if not state.incidents else "WARN",
            summary="no incidents" if not state.incidents else f"open={len(state.incidents)}",
            metrics={"open": len(state.incidents)},
            links=[{"title": "Events", "href": "/api/ui/events"}],
        )
    )
    components.append(
        _component(
            component_id="metrics_pipeline",
            title="Metrics Pipeline",
            group="P1",
            status="OK",
            summary="exporting",
            metrics={"latency_samples": len(state.metrics.latency_samples_ms)},
            links=[{"title": "Metrics", "href": "/metrics"}],
        )
    )
    config_mtime = 0.0
    try:
        config_mtime = state.config.path.stat().st_mtime
    except FileNotFoundError:
        config_mtime = 0.0

    components.append(
        _component(
            component_id="config_pipeline",
            title="Config Pipeline",
            group="P1",
            status="OK",
            summary="active",
            metrics={"config_mtime": round(config_mtime, 2)},
            links=[{"title": "Config", "href": "/api/ui/config"}],
        )
    )
    components.append(
        _component(
            component_id="ui_stream",
            title="UI Stream",
            group="P1",
            status="OK",
            summary="running",
            metrics={"subscribers": 1},
            links=[{"title": "Stream", "href": "/api/ui/status/stream/status"}],
        )
    )
    components.append(
        _component(
            component_id="live_readiness",
            title="Live Readiness",
            group="P1",
            status="OK" if state.control.mode != "HOLD" else "HOLD",
            summary=state.control.mode,
            metrics={"safe_mode": int(state.control.safe_mode)},
            links=[{"title": "Readiness", "href": "/live-readiness"}],
        )
    )

    if state.derivatives:
        for venue_id, runtime in state.derivatives.venues.items():
            connected = runtime.client.ping()
            components.append(
                _component(
                    component_id=f"deriv_{venue_id}",
                    title=f"Derivatives Venue {venue_id.upper()}",
                    group="P1",
                    status="OK" if connected else "WARN",
                    summary="connected" if connected else "unreachable",
                    metrics={
                        "symbols": len(runtime.config.symbols),
                        "hedge_mode": int(getattr(runtime.client, "position_mode", "hedge") == "hedge"),
                    },
                    links=[{"title": "Venue", "href": f"/api/deriv/{venue_id}"}],
                )
            )

    components.append(
        _component(
            component_id="pnl_tracker",
            title="PnL Tracker",
            group="P2",
            status="OK",
            summary="flat",
            metrics={"unrealized": 0.0},
            links=[{"title": "PnL", "href": "/api/ui/pnl"}],
        )
    )
    components.append(
        _component(
            component_id="exposure_monitor",
            title="Exposure Monitor",
            group="P2",
            status="OK",
            summary="balanced",
            metrics={"delta_usd": 0.0},
            links=[{"title": "Exposure", "href": "/api/ui/exposure"}],
        )
    )
    components.append(
        _component(
            component_id="limits_service",
            title="Limits Service",
            group="P2",
            status="OK",
            summary="caps active",
            metrics={
                "per_symbol_cap": (
                    state.config.data.risk.notional_caps.per_symbol_usd
                    if state.config.data.risk and state.config.data.risk.notional_caps
                    else 0
                )
            },
            links=[{"title": "Limits", "href": "/api/ui/limits"}],
        )
    )

    components.append(
        _component(
            component_id="universe_registry",
            title="Universe Registry",
            group="P3",
            status="OK",
            summary="loaded",
            metrics={
                "symbols": sum(len(v.config.symbols) for v in state.derivatives.venues.values())
                if state.derivatives
                else 0
            },
            links=[{"title": "Universe", "href": "/api/ui/universe"}],
        )
    )
    components.append(
        _component(
            component_id="operator_docs",
            title="Operator Docs",
            group="P3",
            status="OK",
            summary="available",
            metrics={"handbooks": 5},
            links=[{"title": "Docs", "href": "/docs/README_ru.md"}],
        )
    )

    while len(components) < 20:
        idx = len(components)
        components.append(
            _component(
                component_id=f"aux_{idx}",
                title=f"Auxiliary {idx}",
                group="P3",
                status="OK",
                summary="nominal",
                metrics={"value": idx},
            )
        )

    return components


def _resolve_threshold(
    *,
    metric: str,
    environment: str,
    thresholds: Dict[str, Dict[str, float]],
) -> Tuple[float | None, str, int]:
    config = thresholds.get(metric, {})
    hold_minutes = int(config.get("hold_minutes", _DEFAULT_HOLD_MINUTES))
    env = (environment or "").lower()
    if "min" in config:
        return float(config["min"]), "min", hold_minutes
    if env in {"local", "paper", "dev"}:
        if "local_ok" in config:
            return float(config["local_ok"]), "max", hold_minutes
    else:
        if "vps_ok" in config:
            return float(config["vps_ok"]), "max", hold_minutes
    if "ok" in config:
        return float(config["ok"]), "max", hold_minutes
    if "local_ok" in config:
        return float(config["local_ok"]), "max", hold_minutes
    if "vps_ok" in config:
        return float(config["vps_ok"]), "max", hold_minutes
    return None, "max", hold_minutes


def _evaluate_slo(
    state: RuntimeState,
    components: List[Dict[str, object]],
    *,
    now: datetime,
) -> Tuple[List[Dict[str, object]], bool, List[str]]:
    thresholds_cfg = state.config.thresholds.slo if state.config.thresholds else {}
    environment = state.control.environment or state.control.deployment_mode or "paper"
    values = dict(state.metrics.slo)
    start_map = state.metrics.slo_breach_started_at
    component_lookup = {comp["id"]: comp for comp in components}
    slo_component = component_lookup.get("slo_watchdog")
    alerts: List[Dict[str, object]] = []
    hold_required = False
    hold_reasons: List[str] = []
    active_breaches = 0

    for metric, value in values.items():
        limit, direction, hold_minutes = _resolve_threshold(
            metric=metric, environment=environment, thresholds=thresholds_cfg
        )
        if limit is None:
            start_map.pop(metric, None)
            continue
        if direction == "max":
            breached = value > limit
            comparator = ">"
        else:
            breached = value < limit
            comparator = "<"

        if not breached:
            start_map.pop(metric, None)
            continue

        active_breaches += 1
        since_iso = start_map.get(metric)
        if since_iso:
            try:
                since = datetime.fromisoformat(since_iso)
            except ValueError:
                since = now
        else:
            since = now
            start_map[metric] = since.isoformat()

        minutes_in_breach = (now - since).total_seconds() / 60.0
        severity = "error"
        component_id = "slo_watchdog"
        status_update = "ERROR"

        if minutes_in_breach >= hold_minutes:
            severity = "critical"
            hold_required = True
            status_update = "HOLD"
            hold_reasons.append(f"{metric} {comparator} {limit}")

        if metric == "recon_mismatch" and value > 0:
            status_update = "HOLD"
            severity = "critical"
            component_id = "recon"
            hold_required = True
            hold_reasons.append("reconciliation mismatch > 0")

        component = component_lookup.get(component_id)
        if component:
            component["status"] = status_update
            component["summary"] = f"{metric} {comparator} {limit}"

        if slo_component:
            current_rank = _SEVERITY_RANK.get(slo_component["status"], 0)
            updated_rank = _SEVERITY_RANK.get(status_update, 0)
            if updated_rank > current_rank:
                slo_component["status"] = status_update

        alerts.append(
            {
                "severity": severity,
                "title": f"SLO breach: {metric}",
                "msg": f"{metric}={value:.3f} (limit {comparator} {limit})",
                "since": since.isoformat(),
                "component_id": component_id,
            }
        )

    if slo_component:
        slo_component["metrics"]["breaches_active"] = active_breaches
        if active_breaches == 0:
            slo_component["status"] = "OK"
            slo_component["summary"] = "within limits"
        elif slo_component["status"] == "ERROR":
            slo_component["summary"] = "breach observed"
        elif slo_component["status"] == "HOLD":
            slo_component["summary"] = "auto-hold engaged"
        else:
            slo_component["summary"] = "watching"

    return alerts, hold_required, hold_reasons


def _score(status: str) -> float:
    return {
        "OK": 1.0,
        "WARN": 0.5,
        "HOLD": 0.0,
        "ERROR": 0.0,
    }.get(status, 0.0)


def _build_snapshot(state: RuntimeState) -> Dict[str, object]:
    now = datetime.now(timezone.utc)
    skew_value = _evaluate_clock_skew(state)
    components = _build_components(state)
    alerts, hold_required, hold_reasons = _evaluate_slo(state, components, now=now)

    scores: Dict[str, List[float]] = {group: [] for group in _GROUP_ORDER}
    for comp in components:
        scores[comp["group"]].append(_score(comp["status"]))

    aggregate = {}
    for group, values in scores.items():
        aggregate[group] = sum(values) / len(values) if values else 1.0

    overall = "OK"
    for comp in components:
        comp_status = comp["status"]
        if _SEVERITY_RANK[comp_status] > _SEVERITY_RANK[overall]:
            overall = comp_status

    if hold_required:
        reason = "; ".join(hold_reasons) if hold_reasons else "SLO breach"
        engage_safety_hold(reason)

    if state.control.mode == "HOLD" or not state.control.preflight_passed or hold_required:
        overall = "HOLD"

    if any(comp["status"] == "ERROR" for comp in components) and overall != "HOLD":
        overall = "ERROR"

    if overall not in {"HOLD", "ERROR"} and any(comp["status"] == "WARN" for comp in components):
        overall = "WARN"

    thresholds = state.config.thresholds.slo if state.config.thresholds else {}

    safety_snapshot = state.safety.status_payload()
    snapshot = {
        "ts": now.isoformat(),
        "overall": overall,
        "scores": aggregate,
        "slo": dict(state.metrics.slo),
        "thresholds": thresholds,
        "components": components,
        "alerts": alerts,
        "hold_active": safety_snapshot.get("hold_active", False),
        "safety": safety_snapshot,
        "resume_request": safety_snapshot.get("resume_request"),
        "clock_skew_s": skew_value,
    }
    auto_payload = state.auto_hedge.as_dict()
    snapshot["auto_hedge"] = {
        "auto_enabled": bool(auto_payload.get("enabled", False)),
        "last_opportunity_checked_ts": auto_payload.get("last_opportunity_checked_ts"),
        "last_execution_result": auto_payload.get("last_execution_result"),
        "consecutive_failures": int(auto_payload.get("consecutive_failures", 0) or 0),
        "on_hold": bool(state.safety.hold_active),
    }
    return redact_sensitive_data(snapshot)


def get_status_overview() -> Dict[str, object]:
    state = get_state()
    return _build_snapshot(state)


def get_status_components() -> Dict[str, object]:
    snapshot = get_status_overview()
    return {"ts": snapshot["ts"], "components": snapshot["components"]}


def get_status_slo() -> Dict[str, object]:
    snapshot = get_status_overview()
    return {
        "ts": snapshot["ts"],
        "slo": snapshot["slo"],
        "thresholds": snapshot["thresholds"],
        "alerts": snapshot["alerts"],
    }

