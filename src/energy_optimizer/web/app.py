"""FastAPI app factory: wires store, service, scheduler, MQTT and static SPA."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..config import Settings, get_settings
from ..scheduler import build_scheduler
from ..service import Service
from ..store import Store
from . import auth as auth_mod
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
        try:
            await service.reconcile_battery_on_startup()
        except Exception:  # pragma: no cover - defensive
            logger.exception("battery startup reconcile failed")
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
            try:
                await service.shutdown_battery_control()
            except Exception:  # pragma: no cover - defensive
                logger.exception("battery shutdown fallback failed")
            service.stop_mqtt()

    app = FastAPI(title="energy-optimizer", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.service = service

    oauth = auth_mod.build_oauth()
    app.state.oauth = oauth

    @app.middleware("http")
    async def require_oidc_session(request: Request, call_next):
        """Gate all routes behind OIDC when enabled."""
        if not auth_mod.oidc_enabled() or oauth is None:
            return await call_next(request)
        if auth_mod.is_public_path(request.url.path):
            return await call_next(request)
        if auth_mod.current_user(request) is not None:
            return await call_next(request)
        return auth_mod.unauthorized_response(request)

    # SessionMiddleware is added last so it is outermost and populates
    # request.session before the auth gate runs. SameSite=lax works with the
    # Authentik redirect; OIDC sessions always use Secure cookies.
    if auth_mod.oidc_enabled():
        app_url = auth_mod.app_url()
        app.add_middleware(
            SessionMiddleware,
            secret_key=auth_mod.session_secret(),
            session_cookie="pvopti_session",
            same_site="lax",
            https_only=app_url.startswith("https://"),
            max_age=60 * 60 * 24 * 7,
        )

    app.include_router(router)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "mode": settings.mode})

    @app.get("/auth/login")
    async def auth_login(request: Request):
        """Start the OIDC authorization-code flow."""
        if not auth_mod.oidc_enabled() or oauth is None:
            raise HTTPException(status_code=404, detail="OIDC is not enabled")
        return await auth_mod.login(request, oauth)

    @app.get("/auth/oidc/callback")
    async def auth_oidc_callback(request: Request):
        """Handle the OIDC provider redirect."""
        if not auth_mod.oidc_enabled() or oauth is None:
            raise HTTPException(status_code=404, detail="OIDC is not enabled")
        return await auth_mod.callback(request, oauth)

    @app.get("/auth/logout")
    async def auth_logout(request: Request):
        """Clear the local session and optionally end the IdP session."""
        if not auth_mod.oidc_enabled():
            raise HTTPException(status_code=404, detail="OIDC is not enabled")
        return await auth_mod.logout(request, oauth)

    @app.get("/api/auth/me")
    async def auth_me(request: Request):
        """Return the signed-in user, or auth-disabled status for the UI."""
        if not auth_mod.oidc_enabled():
            return {"authenticated": False, "oidc_enabled": False, "user": None}
        user = auth_mod.current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return {"authenticated": True, "oidc_enabled": True, "user": user}

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
