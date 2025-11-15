from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, generate_latest, REGISTRY
from starlette.responses import Response

from app.alerts.registry import REGISTRY as alerts_registry
from app.api.ui import alerts as ui_alerts
from app.market.watchdog import watchdog as market_watchdog
from app.ops.status_snapshot import build_ops_snapshot, ops_snapshot_to_dict
from app.readiness.live import registry
from app.recon.background import setup_recon_runner
from app.risk.risk_governor import get_risk_governor
from app.services import runtime
from app.ui.config_snapshot import build_ui_config_snapshot

from .util.logging import setup_logging
from .services.status import get_status_overview, get_status_components, get_status_slo
from .routers import (
    health,
    ui_config,
    opportunities,
    ui_status,
    ui_stream,
    ui_recon,
    ui_exec,
    ui_pnl,
    ui_pnl_attrib,
    ui_exposure,
    ui_control_state,
    live,
    metrics_latency,
    ui_approvals,
    ui_limits,
    ui_risk,
    ui_ops_report,
    ui_partial_hedge,
    ui_router_decisions,
    ui_universe,
    ui_strategy_metrics,
    arb,
    deriv,
    hedge,
)

setup_logging()

app = FastAPI(title="PropBot v6.3.2", version="6.3.2-final")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(health.router, prefix="/api")
app.include_router(live.router, prefix="")
app.include_router(opportunities.router, prefix="/api")
app.include_router(ui_config.router, prefix="/api/ui")
app.include_router(ui_recon.router, prefix="/api/ui/recon")
app.include_router(ui_stream.router, prefix="/api/ui")
app.include_router(ui_status.router, prefix="/api/ui/status")
app.include_router(ui_exec.router, prefix="/api/ui")
app.include_router(ui_pnl.router, prefix="/api/ui")
app.include_router(ui_pnl_attrib.router, prefix="/api/ui")
app.include_router(ui_exposure.router, prefix="/api/ui")
app.include_router(ui_control_state.router, prefix="/api/ui")
app.include_router(ui_approvals.router, prefix="/api/ui")
app.include_router(ui_limits.router, prefix="/api/ui")
app.include_router(ui_universe.router, prefix="/api/ui")
app.include_router(ui_risk.router, prefix="/api/ui")
app.include_router(ui_ops_report.router, prefix="/api/ui")
app.include_router(ui_partial_hedge.router, prefix="/api/ui")
app.include_router(ui_router_decisions.router)
app.include_router(ui_strategy_metrics.router)
app.include_router(ui_alerts.router)
app.include_router(metrics_latency.router, prefix="/metrics")
app.include_router(arb.router, prefix="/api/arb")
app.include_router(deriv.router, prefix="/api/deriv")
app.include_router(hedge.router, prefix="/api/hedge")

setup_recon_runner(app)


@app.get("/api/ui/status")
async def get_ui_status() -> dict[str, Any]:
    """Ops snapshot aggregating router, risk, readiness, watchdog, and alerts."""

    snapshot = build_ops_snapshot(
        router=runtime,
        risk_governor=get_risk_governor(),
        readiness_registry=registry,
        market_watchdog=market_watchdog,
        alerts_registry=alerts_registry,
    )
    payload = ops_snapshot_to_dict(snapshot)
    payload["config"] = build_ui_config_snapshot()
    return payload


@app.get("/api/ui/config")
async def get_ui_config() -> dict[str, Any]:
    """Return the current runtime configuration snapshot for UI."""

    snapshot = build_ui_config_snapshot()
    return {"config": snapshot}


app.router.routes = [
    route
    for route in app.router.routes
    if not (
        getattr(route, "path", None) == "/live-readiness"
        and "GET" in getattr(route, "methods", set())
    )
]


# Prometheus metrics endpoint
@app.get("/metrics")
def metrics() -> Response:
    return Response(
        generate_latest(REGISTRY), media_type="text/plain; version=0.0.4; charset=utf-8"
    )


@app.get("/live-readiness")
def live_readiness() -> JSONResponse:
    status, components = registry.report()
    return JSONResponse({"status": status, "components": components})
