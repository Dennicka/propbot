from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from fastapi.middleware.cors import CORSMiddleware

from . import ledger
from .version import APP_VERSION
from .routers import arb, health, live, risk, ui, ui_config, ui_ops_report, ui_secrets
from .routers import ui_universe
from .routers import ui_strategy
from .routers import ui_status
from .routers import ui_trades
from .routers import ui_risk
from .routers import ui_pnl_attrib
from .routers import exchange_watchdog
from .routers.dashboard import router as dashboard_router
from .utils.idem import IdempotencyCache, IdempotencyMiddleware
from .middlewares.rate import RateLimitMiddleware, RateLimiter
from .telebot import setup_telegram_bot
from .telemetry import observe_ui_latency, setup_slo_monitor
from .auto_hedge_daemon import setup_auto_hedge_daemon
from .startup_validation import validate_startup
from .startup_resume import perform_resume as perform_startup_resume
from services.opportunity_scanner import setup_scanner as setup_opportunity_scanner
from .services.autopilot import setup_autopilot
from .services.orchestrator_alerts import setup_orchestrator_alerts
from .services.exchange_watchdog_runner import setup_exchange_watchdog
from .services.autopilot_guard import setup_autopilot_guard


def _should_guard(request: Request) -> bool:
    if request.method.upper() not in {"POST", "PATCH", "DELETE"}:
        return False
    path = request.url.path
    return path.startswith("/api/ui") or path.startswith("/api/arb")


logger = logging.getLogger("propbot.startup")


def create_app() -> FastAPI:
    ledger.init_db()
    validate_startup()
    resume_ok, resume_payload = perform_startup_resume()
    build_version = os.getenv("BUILD_VERSION") or APP_VERSION
    logger.info(
        "PropBot starting with build_version=%s (app_version=%s)",
        build_version,
        APP_VERSION,
    )
    app = FastAPI(title="PropBot API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    limiter = RateLimiter()
    cache = IdempotencyCache()
    app.state.rate_limiter = limiter
    app.state.idempotency_cache = cache
    app.state.default_rate_limits = (limiter.rate_per_min, limiter.burst)
    app.state.resume_ok = resume_ok
    app.state.resume_payload = resume_payload
    app.add_middleware(RateLimitMiddleware, limiter=limiter, should_guard=_should_guard)
    app.add_middleware(IdempotencyMiddleware, cache=cache, should_guard=_should_guard)

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def _telemetry_middleware(request: Request, call_next):
        start = time.perf_counter()
        path = request.url.path
        try:
            response = await call_next(request)
        except Exception:
            duration = (time.perf_counter() - start) * 1000.0
            if path.startswith("/api/ui"):
                observe_ui_latency(path, duration, status_code=500, error=True)
            raise
        duration = (time.perf_counter() - start) * 1000.0
        if path.startswith("/api/ui"):
            observe_ui_latency(path, duration, status_code=response.status_code)
        return response
    app.include_router(health.router)
    app.include_router(live.router)
    app.include_router(risk.router)
    app.include_router(ui.router)
    app.include_router(ui_secrets.router)
    app.include_router(ui_config.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_universe.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_ops_report.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_pnl_attrib.router, prefix="/api/ui", tags=["ui"])
    app.include_router(exchange_watchdog.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_strategy.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_status.router, prefix="/api/ui/status")
    app.include_router(ui_trades.router)
    app.include_router(ui_risk.router, prefix="/api/ui", tags=["ui"])
    app.include_router(arb.router, prefix="/api/arb", tags=["arb"])
    app.include_router(dashboard_router)
    from .opsbot import setup_notifier as setup_ops_notifier

    setup_ops_notifier(app)
    setup_telegram_bot(app)
    setup_opportunity_scanner(app)
    setup_auto_hedge_daemon(app)
    setup_autopilot(app)
    setup_autopilot_guard(app)
    setup_orchestrator_alerts(app)
    setup_exchange_watchdog(app)
    setup_slo_monitor(app)
    return app


app = create_app()
