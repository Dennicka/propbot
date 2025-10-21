from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import ledger
from .routers import arb, health, ui
from .routers.dashboard import router as dashboard_router


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
    app.include_router(health.router)
    app.include_router(ui.router)
    app.include_router(arb.router, prefix="/api/arb", tags=["arb"])
    app.include_router(dashboard_router)
    return app


app = create_app()
