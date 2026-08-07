from __future__ import annotations

import asyncio

from energy_optimizer.config import Settings
from energy_optimizer.scheduler import build_scheduler
from energy_optimizer.service import Service
from energy_optimizer.store import Store


def test_scheduler_collects_and_controls_ev_every_minute() -> None:
    settings = Settings(db=":memory:", mqtt_enabled=False)
    store = Store(":memory:")
    store.create_all()
    scheduler = build_scheduler(Service(settings, store))

    job = scheduler.get_job("ev_control")

    assert job is not None
    assert str(job.trigger) == "interval[0:01:00]"


async def test_startup_and_optimise_refresh_ev_before_planning_and_control() -> None:
    calls: list[str] = []

    class FakeService:
        settings = Settings(db=":memory:", mqtt_enabled=False)

        async def bootstrap(self):
            calls.append("bootstrap")

        async def collect_telemetry(self):
            calls.append("telemetry")

        async def collect_ev_telemetry(self):
            calls.append("ev")

        async def run_optimise(self):
            calls.append("optimise")

        async def control_ev_charging(self):
            calls.append("control")

        async def refresh_prices(self):
            calls.append("prices")

        async def refresh_meter_values(self):
            calls.append("meter")

    scheduler = build_scheduler(FakeService())  # type: ignore[arg-type]

    await scheduler.get_job("bootstrap").func()
    assert calls == ["bootstrap", "telemetry", "ev", "optimise", "control"]

    calls.clear()
    await scheduler.get_job("optimise").func()
    assert calls == ["ev", "optimise", "control"]

    calls.clear()
    await scheduler.get_job("prices").func()
    assert calls == ["prices", "meter"]


async def test_meter_refresh_runs_even_when_price_refresh_fails() -> None:
    calls: list[str] = []

    class FakeService:
        settings = Settings(db=":memory:", mqtt_enabled=False)

        async def refresh_prices(self):
            calls.append("prices")
            raise RuntimeError("pricing unavailable")

        async def refresh_meter_values(self):
            calls.append("meter")

    scheduler = build_scheduler(FakeService())  # type: ignore[arg-type]

    await scheduler.get_job("prices").func()

    assert calls == ["prices", "meter"]


async def test_ev_control_waits_for_in_progress_optimisation_pipeline() -> None:
    calls: list[str] = []
    optimise_started = asyncio.Event()
    release_optimise = asyncio.Event()

    class FakeService:
        settings = Settings(db=":memory:", mqtt_enabled=False)

        async def collect_ev_telemetry(self):
            calls.append("ev")

        async def run_optimise(self):
            calls.append("optimise")
            optimise_started.set()
            await release_optimise.wait()

        async def control_ev_charging(self):
            calls.append("control")

    scheduler = build_scheduler(FakeService())  # type: ignore[arg-type]
    optimise_task = asyncio.create_task(scheduler.get_job("optimise").func())
    await optimise_started.wait()
    control_task = asyncio.create_task(scheduler.get_job("ev_control").func())
    await asyncio.sleep(0)

    assert calls == ["ev", "optimise"]

    release_optimise.set()
    await asyncio.gather(optimise_task, control_task)
    assert calls == ["ev", "optimise", "control", "ev", "control"]


async def test_ev_control_still_attempts_fail_safe_control_when_collection_fails() -> None:
    calls: list[str] = []

    class FakeService:
        settings = Settings(db=":memory:", mqtt_enabled=False)

        async def collect_ev_telemetry(self):
            calls.append("ev")
            raise RuntimeError("collection failed")

        async def control_ev_charging(self, force_off: bool = False):
            calls.append(f"control:{force_off}")

        async def run_optimise(self):
            calls.append("optimise")

    scheduler = build_scheduler(FakeService())  # type: ignore[arg-type]

    await scheduler.get_job("ev_control").func()
    assert calls == ["ev", "control:True"]

    calls.clear()
    await scheduler.get_job("optimise").func()
    assert calls == ["ev", "control:True"]
