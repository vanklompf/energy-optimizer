"""Entrypoint: run the app with a single Uvicorn worker (avoids duplicate scheduler jobs)."""

from __future__ import annotations

import logging

import uvicorn

from .config import get_settings
from .web import create_app


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.http_host,
        port=settings.http_port,
        workers=1,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
