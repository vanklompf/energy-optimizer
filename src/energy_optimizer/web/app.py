"""FastAPI app factory: wires store, service, scheduler, MQTT and static SPA."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, get_settings
from ..scheduler import build_scheduler
from ..service import Service
from ..store import Store
from .routes import router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, *, run_scheduler: bool = True) -> FastAPI:
    settings = settings or get_settings()
    store = Store(settings.db)
    store.create_all()
    service = Service(settings, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.start_mqtt()
        scheduler = None
        if run_scheduler:
            scheduler = build_scheduler(service)
            scheduler.start()
            logger.info("Scheduler started")
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            service.stop_mqtt()

    app = FastAPI(title="energy-optimizer", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.service = service

    app.include_router(router)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "mode": settings.mode})

    _mount_spa(app)
    return app


def _mount_spa(app: FastAPI) -> None:
    """Serve the built SPA if present; otherwise a placeholder page."""
    if STATIC_DIR.is_dir() and (STATIC_DIR / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str) -> FileResponse:
            candidate = STATIC_DIR / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(STATIC_DIR / "index.html")
    else:

        @app.get("/")
        def placeholder() -> JSONResponse:
            return JSONResponse(
                {
                    "app": "energy-optimizer",
                    "note": "SPA not built. Run `npm run build` in frontend/. API is under /api.",
                }
            )
