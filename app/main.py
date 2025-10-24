from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from . import ledger
from .routers import arb, health, risk, ui
from .routers.dashboard import router as dashboard_router
from .utils.idem import IdempotencyCache, IdempotencyMiddleware
from .middlewares.rate import RateLimitMiddleware, RateLimiter


def _should_guard(request: Request) -> bool:
    if request.method.upper() not in {"POST", "PATCH", "DELETE"}:
        return False
    path = request.url.path
    return path.startswith("/api/ui") or path.startswith("/api/arb")


def create_app() -> FastAPI:
    ledger.init_db()
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
    app.add_middleware(RateLimitMiddleware, limiter=limiter, should_guard=_should_guard)
    app.add_middleware(IdempotencyMiddleware, cache=cache, should_guard=_should_guard)
    app.include_router(health.router)
    app.include_router(risk.router)
    app.include_router(ui.router)
    app.include_router(arb.router, prefix="/api/arb", tags=["arb"])
    app.include_router(dashboard_router)
    return app


app = create_app()
