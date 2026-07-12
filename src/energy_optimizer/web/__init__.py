"""FastAPI application package (REST API + static SPA serving)."""

from .app import create_app

__all__ = ["create_app"]
