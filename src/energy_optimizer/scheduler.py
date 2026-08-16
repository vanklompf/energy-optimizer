"""APScheduler wiring.

Jobs (single Uvicorn process to avoid duplicate jobs):
- collect telemetry: every 1 minute
- refresh Pstryk prices and settled meter values: every 15 minutes
- optimise: every 15 minutes (aligned)
- EV control: every 1 minute
- battery control: short cadence (serialized, coalesce)
- battery heartbeat: independent of optimization
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

from .reports import generate_recent_daily_reports
from .service import Service

logger = logging.getLogger(__name__)


def build_scheduler(service: Service) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=service.settings.tz)
    ev_pipeline_lock = asyncio.Lock()
    cadence = max(1, int(service.settings.battery_control_cadence_seconds))
    heartbeat = max(1, int(service.settings.battery_control_heartbeat_interval_seconds))

    async def _battery_fallback(reason: str) -> None:
        try:
            await service.fallback_battery(reason)
        except Exception:  # pragma: no cover - defensive
            logger.exception("battery fallback failed (%s)", reason)

    async def _collect() -> None:
        try:
            await service.collect_telemetry()
        except Exception:  # pragma: no cover - defensive
            logger.exception("collect_telemetry job failed")
            await _battery_fallback("collect_telemetry_failed")

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
                await _battery_fallback("ev_control_failed")

    async def _battery_control() -> None:
        try:
            await service.control_battery()
        except Exception:  # pragma: no cover
            logger.exception("battery control job failed")
            await _battery_fallback("battery_control_failed")

    async def _battery_heartbeat() -> None:
        try:
            await service.publish_battery_heartbeat()
        except Exception:  # pragma: no cover
            logger.exception("battery heartbeat job failed")

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
            if failed:
                await _battery_fallback("optimise_failed")
            else:
                try:
                    await service.control_battery()
                except Exception:  # pragma: no cover
                    logger.exception("post-optimise battery control failed")
                    await _battery_fallback("post_optimise_battery_control_failed")

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
            if failed:
                await _battery_fallback("bootstrap_failed")
            else:
                try:
                    await service.control_battery()
                except Exception:  # pragma: no cover
                    logger.exception("post-bootstrap battery control failed")
                    await _battery_fallback("post_bootstrap_battery_control_failed")

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
    scheduler.add_job(
        _battery_control,
        IntervalTrigger(seconds=cadence),
        id="battery_control",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _battery_heartbeat,
        IntervalTrigger(seconds=heartbeat),
        id="battery_heartbeat",
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
    async def _daily_report() -> None:
        try:
            generate_recent_daily_reports(service.store, service.settings)
        except Exception:  # pragma: no cover - defensive
            logger.exception("daily report job failed")

    scheduler.add_job(
        _daily_report,
        CronTrigger(hour=0, minute=15),
        id="daily_report",
        max_instances=1,
    )
    return scheduler
