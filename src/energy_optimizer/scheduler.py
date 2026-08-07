"""APScheduler wiring.

Jobs (single Uvicorn process to avoid duplicate jobs):
- collect telemetry: every 1 minute
- refresh Pstryk prices and settled meter values: every 15 minutes
- optimise: every 15 minutes (aligned)
- daily report: 00:15 local

Each job wraps the async service method and swallows/logs exceptions so one failure never
kills the scheduler.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .service import Service

logger = logging.getLogger(__name__)


def build_scheduler(service: Service) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=service.settings.tz)
    ev_pipeline_lock = asyncio.Lock()

    async def _collect() -> None:
        try:
            await service.collect_telemetry()
        except Exception:  # pragma: no cover - defensive
            logger.exception("collect_telemetry job failed")

    async def _prices() -> None:
        try:
            await service.refresh_prices()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Pstryk price refresh job failed")
        try:
            await service.refresh_meter_values()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Pstryk meter refresh job failed")

    async def _ev_control() -> None:
        async with ev_pipeline_lock:
            failed = False
            try:
                await service.collect_ev_telemetry()
            except Exception:  # pragma: no cover
                failed = True
                logger.exception("EV telemetry collection failed; attempting fail-safe control")
            try:
                if failed:
                    await service.control_ev_charging(force_off=True)
                else:
                    await service.control_ev_charging()
            except Exception:  # pragma: no cover
                logger.exception("EV control job failed")

    async def _optimise() -> None:
        async with ev_pipeline_lock:
            failed = False
            try:
                await service.collect_ev_telemetry()
                await service.run_optimise()
            except Exception:  # pragma: no cover
                failed = True
                logger.exception("run_optimise job failed; attempting fail-safe control")
            try:
                if failed:
                    await service.control_ev_charging(force_off=True)
                else:
                    await service.control_ev_charging()
            except Exception:  # pragma: no cover
                logger.exception("post-optimise EV control failed")

    async def _bootstrap() -> None:
        async with ev_pipeline_lock:
            failed = False
            try:
                await service.bootstrap()
                await service.collect_telemetry()
                await service.collect_ev_telemetry()
                await service.run_optimise()
            except Exception:  # pragma: no cover
                failed = True
                logger.exception("bootstrap job failed; attempting fail-safe control")
            try:
                if failed:
                    await service.control_ev_charging(force_off=True)
                else:
                    await service.control_ev_charging()
            except Exception:  # pragma: no cover
                logger.exception("post-bootstrap EV control failed")

    # One-shot backfill at startup (no trigger => runs once, immediately) so backtests and
    # the price chart have history right away rather than only after live collection.
    scheduler.add_job(_bootstrap, id="bootstrap", max_instances=1)
    scheduler.add_job(_collect, IntervalTrigger(minutes=1), id="collect", max_instances=1)
    scheduler.add_job(
        _ev_control,
        IntervalTrigger(minutes=1),
        id="ev_control",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(_prices, IntervalTrigger(minutes=15), id="prices", max_instances=1)
    scheduler.add_job(
        _optimise,
        CronTrigger(minute="0,15,30,45"),
        id="optimise",
        max_instances=1,
        coalesce=True,
    )
    # Daily report placeholder job runs at 00:15 local; report generation is Phase 4.
    scheduler.add_job(_optimise, CronTrigger(hour=0, minute=15), id="daily_report", max_instances=1)
    return scheduler
