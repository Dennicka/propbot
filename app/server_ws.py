from __future__ import annotations
import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, generate_latest, REGISTRY
from starlette.responses import Response
from .util.logging import setup_logging
from .services.status import get_status_overview, get_status_components, get_status_slo
from .routers import health, ui_config, opportunities, ui_status, ui_stream, ui_recon, ui_exec
from .routers import ui_pnl, ui_exposure, ui_control_state, live, metrics_latency
from .routers import ui_approvals, ui_limits, ui_universe

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
app.include_router(ui_exposure.router, prefix="/api/ui")
app.include_router(ui_control_state.router, prefix="/api/ui")
app.include_router(metrics_latency.router, prefix="/metrics")

# Prometheus metrics endpoint
@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(REGISTRY), media_type="text/plain; version=0.0.4; charset=utf-8")
