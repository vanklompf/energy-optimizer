"""Forecast and interval construction used by the optimiser job."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .forecast.load import LoadForecaster, LoadSample
from .forecast.pv import PvForecaster
from .ha_client import HaState
from .optimiser import IntervalInput, OptimiserParams
from .store import Price, PstrykMeterInterval, Store, Telemetry
from .telemetry_energy import complete_hourly_energy

LOAD_LOOKBACK_DAYS = 28
SOLVER_INPUT_SCHEMA = "2"

logger = logging.getLogger(__name__)


class PlanningMixin:
    """Interval grid, forecasts, and solver-input snapshots for Service."""

    settings: Settings
    store: Store

    def _interval_grid(
        self, prices: list[Price], now: dt.datetime | None = None
    ) -> list[tuple[dt.datetime, float]]:
        """Expand hourly prices to aligned future sub-hour intervals."""
        step_h = self.settings.step_hours
        substeps = max(1, int(round(1.0 / step_h)))
        first_start = _next_interval_start(now, self.settings.step_minutes) if now else None
        grid: list[tuple[dt.datetime, float]] = []
        for p in sorted(prices, key=lambda x: x.interval_start):
            if p.buy_gross is None:
                continue
            hour_start = _aware(p.interval_start)
            for k in range(substeps):
                start = hour_start + dt.timedelta(hours=step_h * k)
                if first_start is None or start >= first_start:
                    grid.append((start, step_h))
        return grid

    def _build_intervals(
        self,
        prices: list[Price],
        pv_map: dict[dt.datetime, float],
        load_map: dict[dt.datetime, float],
        *,
        now: dt.datetime | None = None,
        ev_available: bool = False,
        ev_departure_at: dt.datetime | None = None,
    ) -> list[IntervalInput]:
        """Expand prices and attach forecasts plus EV availability/deadline flags."""
        step_h = self.settings.step_hours
        substeps = max(1, int(round(1.0 / step_h)))
        first_start = _next_interval_start(now, self.settings.step_minutes) if now else None
        intervals: list[IntervalInput] = []
        for p in sorted(prices, key=lambda x: x.interval_start):
            if p.buy_gross is None:
                continue
            hour_start = _aware(p.interval_start)
            for k in range(substeps):
                start = hour_start + dt.timedelta(hours=step_h * k)
                if first_start is not None and start < first_start:
                    continue
                intervals.append(
                    IntervalInput(
                        interval_start=start.isoformat(),
                        dt_hours=step_h,
                        pv_energy_kwh=pv_map.get(start, 0.0),
                        load_energy_kwh=load_map.get(start, 0.0),
                        buy_price=float(p.buy_gross),
                        sell_price=float(p.sell_gross or 0.0),
                        price_is_real=(p.source == "api"),
                        ev_available=ev_available,
                        ev_required_soon=bool(
                            ev_available and ev_departure_at and start < ev_departure_at
                        ),
                        ev_opportunistic_allowed=bool(
                            p.is_expensive is not True
                            and (
                                now is None
                                or start.astimezone(ZoneInfo(self.settings.tz)).date()
                                == now.astimezone(ZoneInfo(self.settings.tz)).date()
                            )
                        ),
                    )
                )
        return intervals

    async def _forecast_maps_live(
        self, now: dt.datetime, prices: list[Price]
    ) -> tuple[dict[dt.datetime, float], dict[dt.datetime, float], str | None, str | None]:
        """Compute PV and load forecasts on the optimiser's interval grid (in-memory).

        PV comes from the configured provider (Forecast.Solar); load from the rolling
        hour-of-day/weekday median of stored telemetry. Both return empty when their
        inputs are unavailable so safety can flag the run low-confidence.
        """
        grid = self._interval_grid(prices, now)
        pv_map, pv_conf = await self._pv_forecast_map(grid)
        load_map, load_conf = self._load_forecast_map(now, grid)
        return pv_map, load_map, pv_conf, load_conf

    async def _pv_forecast_map(
        self, grid: list[tuple[dt.datetime, float]]
    ) -> tuple[dict[dt.datetime, float], str | None]:
        s = self.settings
        if s.pv_forecast_provider == "none" or not s.pv_planes or not grid:
            return {}, None
        try:
            async with PvForecaster(
                s.pv_lat,
                s.pv_lon,
                s.pv_planes,
                provider=s.pv_forecast_provider,
                solcast_api_key=s.solcast_api_key,
            ) as pvf:
                points = await pvf.forecast()
        except Exception:  # pragma: no cover - network dependent
            logger.warning("PV forecast failed", exc_info=True)
            return {}, None
        if not points:
            return {}, None
        hourly = {_aware(p.interval_start): p.energy_kwh for p in points}
        conf = "ok" if all(p.confidence == "ok" for p in points) else "low_confidence"
        # Distribute each hour's energy across its sub-hour steps proportionally to dt.
        out: dict[dt.datetime, float] = {}
        for start, dt_hours in grid:
            hour = start.replace(minute=0, second=0, microsecond=0)
            energy = hourly.get(hour)
            if energy is not None:
                out[start] = energy * dt_hours
        return out, conf

    def _load_forecast_map(
        self, now: dt.datetime, grid: list[tuple[dt.datetime, float]]
    ) -> tuple[dict[dt.datetime, float], str | None]:
        samples = self._load_samples(now)
        if not samples or not grid:
            return {}, None
        points = LoadForecaster(tz=self.settings.tz, lookback_days=LOAD_LOOKBACK_DAYS).forecast(
            samples, grid
        )
        out = {_aware(p.interval_start): p.load_kwh for p in points}
        conf = "ok" if all(p.confidence == "ok" for p in points) else "low_confidence"
        return out, conf

    def _load_samples(self, now: dt.datetime) -> list[LoadSample]:
        """Hourly load history, reconciled to Pstryk's settlement boundary when available."""
        lookback = now - dt.timedelta(days=LOAD_LOOKBACK_DAYS)
        with self.store.session() as session:
            rows = (
                session.execute(
                    select(Telemetry).where(Telemetry.ts >= lookback).order_by(Telemetry.ts)
                )
                .scalars()
                .all()
            )
            meter_rows = (
                session.execute(
                    select(PstrykMeterInterval)
                    .where(PstrykMeterInterval.interval_start >= lookback)
                    .where(PstrykMeterInterval.interval_start < now)
                )
                .scalars()
                .all()
            )
        meter_by_hour = {
            _aware(row.interval_start).replace(minute=0, second=0, microsecond=0): row
            for row in meter_rows
        }
        energy_by_hour = complete_hourly_energy(rows)

        samples: list[LoadSample] = []
        for hour, energy in sorted(energy_by_hour.items()):
            meter = meter_by_hour.get(hour)
            if meter is None:
                continue

            # Settlement-boundary balance. Pstryk import/export replace Sigen's grid
            # channels; Sigen remains the source for covered PV and battery energy.
            corrected_load = max(
                0.0,
                energy["pv"]
                + meter.import_kwh
                + energy["discharge"]
                - meter.export_kwh
                - energy["charge"],
            )
            samples.append(LoadSample(ts=hour, load_kw=corrected_load))
        return samples

    def _solver_input_snapshot(
        self, intervals: list[IntervalInput], soc_start_kwh: float, params: OptimiserParams
    ) -> dict[str, object]:
        return {
            "schema": SOLVER_INPUT_SCHEMA,
            "soc_start_kwh": soc_start_kwh,
            "params": asdict(params),
            "intervals": [asdict(i) for i in intervals],
        }


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _next_interval_start(value: dt.datetime, step_minutes: int) -> dt.datetime:
    value = _aware(value)
    floor = value.replace(
        minute=(value.minute // step_minutes) * step_minutes,
        second=0,
        microsecond=0,
    )
    if (value - floor).total_seconds() <= 60:
        return floor
    return floor + dt.timedelta(minutes=step_minutes)


def hourly_from_map(values: dict[dt.datetime, float]) -> dict[dt.datetime, float]:
    """Aggregate a sub-hour-step map into hourly sums for compact forecast persistence."""
    out: dict[dt.datetime, float] = {}
    for ts, value in values.items():
        hour = _aware(ts).replace(minute=0, second=0, microsecond=0)
        out[hour] = out.get(hour, 0.0) + value
    return out


def soc_pct_or_reserve(soc_pct: float | None, reserve_pct: float) -> float:
    return reserve_pct if soc_pct is None else soc_pct


def has_complete_telemetry_coverage(
    session: Session, start: dt.datetime, end: dt.datetime
) -> bool:
    """Whether every completed UTC hour has integrable PV/battery telemetry.

    A few samples at the beginning of a window do not establish a safe financial
    reconciliation history: one later recorder gap invalidates that hour. Keep the
    bootstrap read-only but fill any incomplete window on the next restart.
    """
    start = _aware(start).replace(minute=0, second=0, microsecond=0)
    end = _aware(end).replace(minute=0, second=0, microsecond=0)
    if end <= start:
        return True
    rows = session.execute(
        select(Telemetry).where(Telemetry.ts >= start).where(Telemetry.ts < end)
    ).scalars().all()
    covered = complete_hourly_energy(rows)
    hour = start
    while hour < end:
        if hour not in covered:
            return False
        hour += dt.timedelta(hours=1)
    return True


def regular_state_samples(
    states: list[HaState], start: dt.datetime, end: dt.datetime
) -> dict[dt.datetime, float]:
    """Resample recorder states every five minutes using time-correct hold semantics."""
    points = sorted(
        (
            (_aware(timestamp), state.as_float())
            for state in states
            if (timestamp := state.last_updated) is not None
        ),
        key=lambda item: item[0],
    )
    if not points:
        return {}

    samples: dict[dt.datetime, float] = {}
    tick = _aware(start)
    end = _aware(end)
    index = 0
    current: float | None = None
    while tick < end:
        while index < len(points) and points[index][0] <= tick:
            current = points[index][1]
            index += 1
        if current is not None:
            samples[tick] = current
        tick += dt.timedelta(minutes=5)
    return samples

