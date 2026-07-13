from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from energy_optimizer.config import Settings
from energy_optimizer.ha_client import HaState
from energy_optimizer.service import (
    Service,
    _hourly_from_map,
    _hourly_mean_states,
)
from energy_optimizer.store import PlanStep, Price, Run, Store, Telemetry, utcnow


def _settings() -> Settings:
    # pv_forecast_provider="none" keeps the optimiser path fully offline (no Forecast.Solar).
    return Settings(
        db=":memory:",
        mqtt_enabled=False,
        ha_token="",
        pstryk_api_key="",
        pv_forecast_provider="none",
        pv_planes=[],
        battery_capacity_kwh=10.0,
        battery_max_charge_kw=5.0,
        battery_max_discharge_kw=5.0,
        battery_soc_min_pct=20.0,
        battery_soc_max_pct=98.0,
        battery_round_trip_efficiency=0.90,
        degradation_cost_pln_per_kwh=0.05,
        site_import_limit_kw=14.0,
        site_export_limit_kw=14.0,
        inverter_limit_kw=12.0,
    )


async def test_run_optimise_produces_plan_from_load_forecast() -> None:
    """With fresh telemetry, a current-hour price and telemetry history for the load
    forecast, a run is no longer blocked and emits plan steps (regression for the
    nighttime-stale blocker + forecast wiring)."""
    settings = _settings()
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)

    now = utcnow()
    floor = now.replace(minute=0, second=0, microsecond=0)
    with store.session() as session:
        for h in range(6):
            buy = 0.5 if h < 3 else 2.0
            session.add(
                Price(
                    interval_start=floor + dt.timedelta(hours=h),
                    buy_gross=buy,
                    full_price=buy,
                    sell_gross=buy * 0.5,
                    source="api",
                )
            )
        # Two days of hourly telemetry so the load forecaster has samples; the most recent
        # sample is "now" (fresh) with PV pinned at 0 (nighttime steady state).
        for h in range(48):
            session.add(
                Telemetry(
                    ts=now - dt.timedelta(hours=h),
                    soc_pct=50.0,
                    pv_kw=0.0,
                    load_kw=1.0,
                    grid_import_kw=1.0,
                    grid_export_kw=0.0,
                    batt_charge_kw=0.0,
                    batt_discharge_kw=0.0,
                    stale=False,
                )
            )

    run_id = await service.run_optimise()

    with store.session() as session:
        run = session.get(Run, run_id)
        steps = (
            session.execute(select(PlanStep).where(PlanStep.run_id == run_id)).scalars().all()
        )
    assert run is not None
    assert run.status != "blocked"
    assert len(steps) > 0


def test_hourly_from_map_sums_substeps_into_hours() -> None:
    base = dt.datetime(2026, 7, 13, 10, 0, tzinfo=dt.UTC)
    values = {
        base: 0.25,
        base + dt.timedelta(minutes=15): 0.25,
        base + dt.timedelta(minutes=30): 0.25,
        base + dt.timedelta(minutes=45): 0.25,
        base + dt.timedelta(hours=1): 1.0,
    }
    hourly = _hourly_from_map(values)
    assert hourly[base] == 1.0
    assert hourly[base + dt.timedelta(hours=1)] == 1.0


def test_hourly_mean_states_buckets_by_hour() -> None:
    hour = dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC)

    def state(minute: int, value: str) -> HaState:
        return HaState(
            entity_id="sensor.x",
            state=value,
            last_updated=hour + dt.timedelta(minutes=minute),
            attributes={},
        )

    means = _hourly_mean_states([state(0, "2.0"), state(30, "4.0"), state(45, "unknown")])
    assert means[hour] == 3.0
