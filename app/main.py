from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import ledger
from .version import APP_VERSION
from .routers import (
    arb,
    health,
    live,
    risk,
    ui,
    ui_incident,
    ui_config,
    recon,
    ui_ops_report,
    ui_partial_hedge,
    ui_recon,
    ui_router_decisions,
    ui_secrets,
    ui_exec,
)
from .routers import ui_universe
from .routers import ui_strategy
from .routers import ui_strategy_metrics
from .routers import ui_status
from .routers import ui_trades
from .routers import ui_risk
from .routers import ui_pnl_attrib
from .routers import exchange_watchdog
from .routers.dashboard import router as dashboard_router
from .api.ui import alerts as ui_alerts
from .api.ui import pretrade as ui_pretrade
from .api.ui import readiness as ui_readiness
from .api.ui import system_status as ui_system_status
from .utils.idem import IdempotencyCache, IdempotencyMiddleware
from .utils.static import CachedStaticFiles
from .middlewares.rate import RateLimitMiddleware, RateLimiter
from .telebot import setup_telegram_bot
from .telemetry import observe_ui_latency, setup_slo_monitor
from .readiness import (
    DEFAULT_POLL_INTERVAL as READINESS_POLL_INTERVAL,
    READINESS_AGGREGATOR,
    collect_readiness_signals,
    wait_for_live_readiness,
)
from .readiness.live import registry as readiness_registry
from .alerts.registry import REGISTRY as alerts_registry
from .market.watchdog import watchdog as market_watchdog
from .ops.status_snapshot import build_ops_snapshot, ops_snapshot_to_dict
from .risk.risk_governor import get_risk_governor
from .ui.config_snapshot import build_ui_config_snapshot
from .metrics.observability import observe_api_latency, register_slo_metrics
from .auto_hedge_daemon import setup_auto_hedge_daemon
from .startup_validation import validate_startup
from .profile_config import ProfileConfigError, load_profile_config
from .config.profiles import resolve_guard_status
from .startup_resume import perform_resume as perform_startup_resume
from services.opportunity_scanner import setup_scanner as setup_opportunity_scanner
from .services.autopilot import setup_autopilot
from .services.orchestrator_alerts import setup_orchestrator_alerts
from .services.exchange_watchdog_runner import setup_exchange_watchdog
from .services.autopilot_guard import setup_autopilot_guard
from .services.partial_hedge_runner import setup_partial_hedge_runner
from .services.recon_runner import setup_recon_runner
from .services import runtime as runtime_service
from .services.trading_profile import get_trading_profile
from .execution.stuck_order_resolver import setup_stuck_resolver


def _should_guard(request: Request) -> bool:
    if request.method.upper() not in {"POST", "PATCH", "DELETE"}:
        return False
    path = request.url.path
    return path.startswith("/api/ui") or path.startswith("/api/arb")


logger = logging.getLogger("propbot.startup")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _startup_timeout_seconds(state) -> float:
    default_timeout = 120.0
    config = getattr(getattr(state, "config", None), "data", None)
    if config is not None:
        readiness_cfg = getattr(config, "readiness", None)
        if readiness_cfg is not None:
            value = getattr(readiness_cfg, "startup_timeout_sec", None)
            if value is not None:
                try:
                    return max(float(value), 1.0)
                except (TypeError, ValueError):  # pragma: no cover - defensive
                    logger.debug("invalid readiness.startup_timeout_sec=%r", value)
    raw = os.getenv("READINESS_STARTUP_TIMEOUT_SEC")
    if raw:
        try:
            return max(float(raw), 1.0)
        except ValueError:  # pragma: no cover - defensive
            logger.debug("invalid READINESS_STARTUP_TIMEOUT_SEC=%s", raw)
    return default_timeout


def create_app() -> FastAPI:
    ledger.init_db()
    validate_startup()
    profile = get_trading_profile()
    logger.info(
        "Trading profile active: %s limits(order=%s symbol=%s global=%s daily_loss=%s) allow_new_orders=%s closures_only=%s",
        profile.name,
        profile.max_notional_per_order,
        profile.max_notional_per_symbol,
        profile.max_notional_global,
        profile.daily_loss_limit,
        profile.allow_new_orders,
        profile.allow_closures_only,
    )
    try:
        profile_cfg = load_profile_config()
    except ProfileConfigError as exc:
        logger.warning("Не удалось загрузить профиль запуска: %s", exc)
    else:
        logger.info(
            "Активный профиль=%s dry_run=%s risk_limits(total=%s, single=%s, daily_loss_usd=%s, drawdown_bps=%s) flags=%s",
            profile_cfg.name,
            profile_cfg.dry_run,
            profile_cfg.risk_limits.max_total_notional_usd,
            profile_cfg.risk_limits.max_single_position_usd,
            profile_cfg.risk_limits.daily_loss_cap_usd,
            profile_cfg.risk_limits.max_drawdown_bps,
            profile_cfg.flags.as_dict(),
        )
        guards = resolve_guard_status(profile_cfg)
        logger.info(
            "Guard toggles: slo=%s health=%s hedge=%s (partial=%s auto=%s) recon=%s watchdog=%s",
            guards.get("slo"),
            guards.get("health"),
            guards.get("hedge"),
            guards.get("partial_hedge"),
            guards.get("auto_hedge"),
            guards.get("recon"),
            guards.get("watchdog"),
        )
    resume_ok, resume_payload = perform_startup_resume()
    build_version = os.getenv("BUILD_VERSION") or APP_VERSION
    logger.info(
        "PropBot starting with build_version=%s (app_version=%s)",
        build_version,
        APP_VERSION,
    )
    try:
        state = runtime_service.get_state()
    except Exception:  # pragma: no cover - defensive logging
        logger.debug("Не удалось получить runtime state для логирования", exc_info=True)
    else:
        control = getattr(state, "control", None)
        if control is not None:
            mode = str(getattr(control, "mode", "HOLD") or "HOLD").upper()
            hold_active = mode != "RUN"
            logger.info(
                "Control state: mode=%s hold=%s safe_mode=%s dry_run=%s dry_run_mode=%s two_man=%s",
                mode,
                hold_active,
                getattr(control, "safe_mode", True),
                getattr(control, "dry_run", False),
                getattr(control, "dry_run_mode", False),
                getattr(control, "two_man_rule", True),
            )

    app = FastAPI(title="PropBot API")
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.is_dir():
        app.mount("/static", CachedStaticFiles(directory=str(static_dir)), name="static")
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
        register_slo_metrics()
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    def _route_label(request: Request) -> str:
        route = request.scope.get("route")
        if route is not None:
            for attr in ("path", "path_format", "path_regex"):
                candidate = getattr(route, attr, None)
                if isinstance(candidate, str) and candidate:
                    return candidate
        return request.url.path

    @app.middleware("http")
    async def _telemetry_middleware(request: Request, call_next):
        start = time.perf_counter()
        path = request.url.path
        method = request.method
        route = _route_label(request)
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_s = max(time.perf_counter() - start, 0.0)
            observe_api_latency(route, method, 500, duration_s)
            if path.startswith("/api/ui"):
                observe_ui_latency(path, duration_s * 1000.0, status_code=500, error=True)
            logger.exception(
                "unhandled request error",
                extra={"route": route, "method": method, "error": str(exc)},
            )
            raise
        duration_s = max(time.perf_counter() - start, 0.0)
        observe_api_latency(route, method, response.status_code, duration_s)
        if path.startswith("/api/ui"):
            observe_ui_latency(path, duration_s * 1000.0, status_code=response.status_code)
        return response

    app.include_router(health.router)
    app.include_router(live.router)
    app.include_router(risk.router)
    app.include_router(ui.router)
    app.include_router(ui_secrets.router)
    app.include_router(ui_config.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_exec.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_universe.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_ops_report.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_partial_hedge.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_pnl_attrib.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_recon.router, prefix="/api/ui/recon", tags=["ui"])
    app.include_router(ui_router_decisions.router)
    app.include_router(ui_incident.router)
    app.include_router(recon.router)
    app.include_router(exchange_watchdog.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_strategy.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_strategy_metrics.router)
    app.include_router(ui_status.router, prefix="/api/ui/status")
    app.include_router(ui_alerts.router)
    app.include_router(ui_trades.router)
    app.include_router(ui_risk.router, prefix="/api/ui", tags=["ui"])
    app.include_router(ui_pretrade.router)
    app.include_router(ui_readiness.router)
    app.include_router(ui_system_status.router)
    app.include_router(arb.router, prefix="/api/arb", tags=["arb"])
    app.include_router(dashboard_router)
    from .opsbot import setup_notifier as setup_ops_notifier

    @app.get("/api/ui/status")
    async def get_ui_status() -> dict[str, Any]:
        """Ops snapshot aggregating router, risk, readiness, watchdog, and alerts."""

        snapshot = build_ops_snapshot(
            router=runtime_service,
            risk_governor=get_risk_governor(),
            readiness_registry=readiness_registry,
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

    setup_ops_notifier(app)
    setup_telegram_bot(app)
    setup_opportunity_scanner(app)
    setup_auto_hedge_daemon(app)
    setup_autopilot(app)
    setup_autopilot_guard(app)
    setup_orchestrator_alerts(app)
    setup_exchange_watchdog(app)
    setup_recon_runner(app)
    setup_partial_hedge_runner(app)
    setup_stuck_resolver(app)
    setup_slo_monitor(app)

    @app.on_event("startup")
    async def _install_shutdown_handlers() -> None:  # pragma: no cover - integration glue
        try:
            runtime_service.setup_signal_handlers(asyncio.get_running_loop())
        except RuntimeError:  # pragma: no cover - no running loop
            logger.debug("signal handler installation skipped: no running loop")

    @app.on_event("startup")
    async def _wait_for_readiness_gate() -> None:
        state = runtime_service.get_state()
        control = getattr(state, "control", None)
        environment = str(
            getattr(control, "environment", None) or getattr(control, "deployment_mode", "") or ""
        ).lower()
        default_wait = environment in {"live", "testnet"}
        if not _env_flag("WAIT_FOR_LIVE_READINESS_ON_START", default_wait):
            return
        mode = str(getattr(control, "mode", "HOLD") or "HOLD").upper()
        if mode != "RUN":
            return
        timeout_s = _startup_timeout_seconds(state)
        target_safe_mode = bool(getattr(control, "safe_mode", True))
        runtime_service.engage_safety_hold("startup:wait_for_readiness", source="bootstrap")
        ready = await wait_for_live_readiness(
            READINESS_AGGREGATOR,
            collect_readiness_signals,
            interval_s=READINESS_POLL_INTERVAL,
            timeout_s=timeout_s,
            log=logger,
        )
        if ready:
            runtime_service.autopilot_apply_resume(safe_mode=target_safe_mode)
        else:
            logger.warning(
                "wait-for-readiness timeout reached; system remains in HOLD",
            )

    return app


app = create_app()
