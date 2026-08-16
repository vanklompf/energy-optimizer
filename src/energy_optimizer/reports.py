"""Settled-series loading and daily economic reports."""

from __future__ import annotations

import datetime as dt
import logging
import math
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import Settings
from .optimiser import IntervalInput, OptimiserParams, optimise
from .simulator import (
    BatteryParams,
    SeriesInterval,
    get_policy,
    simulate_policy,
    value_actual,
    value_optimiser_plan,
)
from .store import DailyReport, Forecast, Price, PstrykMeterInterval, Store, Telemetry
from .telemetry_energy import complete_hourly_energy

logger = logging.getLogger(__name__)


class IncompleteSeriesError(ValueError):
    """Settled Pstryk data cannot be compared without complete aligned inputs."""


def load_settled_series(store: Store, start: dt.datetime, end: dt.datetime) -> list[SeriesInterval]:
    """Build a contiguous, coverage-checked settled hourly series."""
    with store.session() as session:
        prices = (
            session.execute(
                select(Price)
                .where(Price.interval_start >= start)
                .where(Price.interval_start < end)
                .order_by(Price.interval_start)
            )
            .scalars()
            .all()
        )
        telem = (
            session.execute(
                select(Telemetry)
                .where(Telemetry.ts >= start)
                .where(Telemetry.ts < end)
                .order_by(Telemetry.ts)
            )
            .scalars()
            .all()
        )
        meter_rows = (
            session.execute(
                select(PstrykMeterInterval)
                .where(PstrykMeterInterval.interval_start >= start)
                .where(PstrykMeterInterval.interval_end <= end)
                .order_by(PstrykMeterInterval.interval_start)
            )
            .scalars()
            .all()
        )
    if not meter_rows:
        return []
    expected_start = ceil_hour(start)
    expected_end = aware(end).replace(minute=0, second=0, microsecond=0)
    first_start = aware(meter_rows[0].interval_start)
    last_end = aware(meter_rows[-1].interval_end)
    if first_start != expected_start:
        raise IncompleteSeriesError(f"missing leading settlement before {first_start.isoformat()}")
    if last_end != expected_end:
        raise IncompleteSeriesError(f"missing trailing settlement after {last_end.isoformat()}")

    energy_by_hour = complete_hourly_energy(telem)
    price_by_hour = {
        aware(row.interval_start).replace(minute=0, second=0, microsecond=0): row
        for row in prices
        if row.buy_gross is not None
    }
    series: list[SeriesInterval] = []
    expected_hour: dt.datetime | None = None
    for billing in meter_rows:
        hour = aware(billing.interval_start).replace(minute=0, second=0, microsecond=0)
        interval_end = aware(billing.interval_end)
        if interval_end - hour != dt.timedelta(hours=1):
            raise IncompleteSeriesError(f"non-hourly Pstryk interval at {hour.isoformat()}")
        if expected_hour is not None and hour != expected_hour:
            raise IncompleteSeriesError(f"settlement gap before {hour.isoformat()}")
        expected_hour = hour + dt.timedelta(hours=1)

        price = price_by_hour.get(hour)
        energy = energy_by_hour.get(hour)
        if price is None or price.buy_gross is None or price.sell_gross is None:
            raise IncompleteSeriesError(f"missing settled price at {hour.isoformat()}")
        if energy is None:
            raise IncompleteSeriesError(f"incomplete live telemetry at {hour.isoformat()}")

        corrected_load = max(
            0.0,
            energy["pv"]
            + billing.import_kwh
            + energy["discharge"]
            - billing.export_kwh
            - energy["charge"],
        )
        series.append(
            SeriesInterval(
                interval_start=hour.isoformat(),
                dt_hours=1.0,
                pv_energy_kwh=energy["pv"],
                load_energy_kwh=corrected_load,
                buy_price=float(price.buy_gross),
                sell_price=float(price.sell_gross),
                measured_grid_import_kwh=billing.import_kwh,
                measured_grid_export_kwh=billing.export_kwh,
                measured_charge_kwh=energy["charge"],
                measured_discharge_kwh=energy["discharge"],
            )
        )
    return series


def battery_params(settings: Settings, overrides: dict[str, float]) -> BatteryParams:
    cap = overrides.get("capacity_kwh", settings.battery_capacity_kwh)
    return BatteryParams(
        capacity_kwh=cap,
        soc_min_kwh=overrides.get("soc_min_kwh", settings.soc_min_kwh),
        soc_max_kwh=overrides.get("soc_max_kwh", settings.soc_max_kwh),
        max_charge_kw=overrides.get("max_charge_kw", settings.battery_max_charge_kw),
        max_discharge_kw=overrides.get("max_discharge_kw", settings.battery_max_discharge_kw),
        eta_charge=settings.eta_charge,
        eta_discharge=settings.eta_discharge,
        degradation_cost_pln_per_kwh=settings.degradation_cost_pln_per_kwh,
        import_price_adjustment_pln_kwh=settings.import_price_adjustment_pln_kwh,
    )


def optimiser_params(settings: Settings, overrides: dict[str, float]) -> OptimiserParams:
    return OptimiserParams(
        battery_capacity_kwh=overrides.get("capacity_kwh", settings.battery_capacity_kwh),
        soc_min_kwh=overrides.get("soc_min_kwh", settings.soc_min_kwh),
        battery_hard_min_kwh=overrides.get("battery_hard_min_kwh", settings.hard_soc_min_kwh),
        soc_max_kwh=overrides.get("soc_max_kwh", settings.soc_max_kwh),
        max_charge_kw=overrides.get("max_charge_kw", settings.battery_max_charge_kw),
        max_discharge_kw=overrides.get("max_discharge_kw", settings.battery_max_discharge_kw),
        eta_charge=settings.eta_charge,
        eta_discharge=settings.eta_discharge,
        site_import_limit_kw=settings.site_import_limit_kw,
        site_export_limit_kw=settings.site_export_limit_kw,
        inverter_limit_kw=settings.inverter_limit_kw,
        degradation_cost_pln_per_kwh=settings.degradation_cost_pln_per_kwh,
        import_price_adjustment_pln_kwh=settings.import_price_adjustment_pln_kwh,
        allow_battery_export=settings.allow_battery_export,
        allow_grid_charging=settings.allow_grid_charging,
        activation_margin_pln_kwh=overrides.get(
            "activation_margin_pln_kwh", settings.battery_control_activation_margin_pln_kwh
        ),
        grid_charge_margin_pln_kwh=overrides.get(
            "grid_charge_margin_pln_kwh", settings.grid_charge_margin_pln_kwh
        ),
        minimum_export_spread_pln_kwh=overrides.get(
            "minimum_export_spread_pln_kwh", settings.minimum_export_spread_pln_kwh
        ),
    )


def soc_start_kwh(store: Store, start: dt.datetime, settings: Settings) -> float:
    tolerance = dt.timedelta(minutes=15)
    with store.session() as session:
        rows = (
            session.execute(
                select(Telemetry)
                .where(Telemetry.ts >= start - tolerance)
                .where(Telemetry.ts <= start + tolerance)
                .where(Telemetry.stale.is_(False))
                .where(Telemetry.soc_pct.is_not(None))
            )
            .scalars()
            .all()
        )
    rows = [
        row
        for row in rows
        if row.soc_pct is not None and math.isfinite(row.soc_pct) and 0 <= row.soc_pct <= 100
    ]
    if not rows:
        raise IncompleteSeriesError(f"missing fresh battery SoC near {start.isoformat()}")
    row = min(rows, key=lambda item: abs(aware(item.ts) - start))
    assert row.soc_pct is not None
    return row.soc_pct / 100.0 * settings.battery_capacity_kwh


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return aware(value).isoformat()


def aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def ceil_hour(value: dt.datetime) -> dt.datetime:
    instant = aware(value)
    floor = instant.replace(minute=0, second=0, microsecond=0)
    return floor if instant == floor else floor + dt.timedelta(hours=1)


def parse_dt(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def generate_daily_report(
    store: Store,
    settings: Settings,
    day: dt.date | None = None,
    *,
    now: dt.datetime | None = None,
) -> DailyReport | None:
    """Persist one local-calendar-day economic report from settled Pstryk data."""
    zone = ZoneInfo(settings.tz)
    now = now or dt.datetime.now(tz=dt.UTC)
    local_now = now.astimezone(zone)
    if day is None:
        day = (local_now.date() - dt.timedelta(days=1))
    start = dt.datetime.combine(day, dt.time.min, tzinfo=zone).astimezone(dt.UTC)
    end = start + dt.timedelta(days=1)
    try:
        series = load_settled_series(store, start, end)
    except IncompleteSeriesError as exc:
        logger.info("daily report %s skipped: %s", day.isoformat(), exc)
        return None
    if not series:
        logger.info("daily report %s skipped: no settled meter data", day.isoformat())
        return None

    battery = battery_params(settings, {})
    settled_start = parse_dt(series[0].interval_start)
    try:
        soc0 = soc_start_kwh(store, settled_start, settings)
    except IncompleteSeriesError as exc:
        logger.info("daily report %s skipped: %s", day.isoformat(), exc)
        return None

    actual = value_actual(series, battery)
    actual_cost = actual.cost
    pv = simulate_policy(series, get_policy("pv_only"), soc0, battery)
    selfcons = simulate_policy(series, get_policy("self_consumption"), soc0, battery)
    opt = optimise(_series_to_intervals(series), soc0, optimiser_params(settings, {}))
    opt_cost = (
        value_optimiser_plan(opt, series, battery) if opt.status == "optimal" else None
    )

    actual_net = actual_cost.net_cost_pln if actual_cost is not None else None
    opt_net = opt_cost.net_cost_pln if opt_cost is not None else None
    missed = (
        actual_net - opt_net if actual_net is not None and opt_net is not None else None
    )
    cycles = None
    if actual_cost is not None and settings.battery_capacity_kwh > 0:
        cycles = actual_cost.battery_throughput_kwh / (2.0 * settings.battery_capacity_kwh)

    pv_mae, load_mae = _forecast_mae(store, start, end)
    row = DailyReport(
        date=day.isoformat(),
        actual_cost_pln=_round(actual_net),
        optimizer_sim_cost_pln=_round(opt_net),
        pvonly_cost_pln=_round(pv.cost.net_cost_pln if pv.cost else None),
        selfcons_cost_pln=_round(selfcons.cost.net_cost_pln if selfcons.cost else None),
        missed_opportunity_pln=_round(missed),
        actual_import_kwh=_round(actual_cost.import_kwh if actual_cost else None),
        actual_export_kwh=_round(actual_cost.export_kwh if actual_cost else None),
        battery_cycles=_round(cycles),
        degradation_cost_pln=_round(actual_cost.degradation_cost_pln if actual_cost else None),
        pv_forecast_mae_kwh=_round(pv_mae),
        load_forecast_mae_kwh=_round(load_mae),
        forecast_error_cost_pln=None,
    )
    with store.session() as session:
        session.merge(row)
    logger.info("daily report %s actual=%s optimiser=%s", day.isoformat(), actual_net, opt_net)
    return row


def generate_recent_daily_reports(store: Store, settings: Settings) -> list[DailyReport]:
    """Write yesterday and the day before (settlement often lags one day)."""
    zone = ZoneInfo(settings.tz)
    today = dt.datetime.now(tz=dt.UTC).astimezone(zone).date()
    written: list[DailyReport] = []
    for offset in (1, 2):
        row = generate_daily_report(store, settings, today - dt.timedelta(days=offset))
        if row is not None:
            written.append(row)
    return written


def _series_to_intervals(series: list[SeriesInterval]) -> list[IntervalInput]:
    return [
        IntervalInput(
            interval_start=s.interval_start,
            dt_hours=s.dt_hours,
            pv_energy_kwh=s.pv_energy_kwh,
            load_energy_kwh=s.load_energy_kwh,
            buy_price=s.buy_price,
            sell_price=s.sell_price,
        )
        for s in series
    ]


def _forecast_mae(
    store: Store, start: dt.datetime, end: dt.datetime
) -> tuple[float | None, float | None]:
    with store.session() as session:
        forecasts = (
            session.execute(
                select(Forecast)
                .where(Forecast.interval_start >= start)
                .where(Forecast.interval_start < end)
            )
            .scalars()
            .all()
        )
        telem = (
            session.execute(
                select(Telemetry)
                .where(Telemetry.ts >= start)
                .where(Telemetry.ts < end)
                .where(Telemetry.stale.is_(False))
            )
            .scalars()
            .all()
        )
    if not forecasts:
        return None, None
    energy = complete_hourly_energy(telem)
    pv_err: list[float] = []
    load_err: list[float] = []
    for row in forecasts:
        hour = aware(row.interval_start).replace(minute=0, second=0, microsecond=0)
        actual = energy.get(hour)
        if actual is None:
            continue
        if row.kind == "pv":
            pv_err.append(abs(row.value - actual["pv"]))
        elif row.kind == "load":
            continue  # live telemetry has no settlement-reconstructed load channel here
    # Load in complete_hourly_energy may not have a load key — skip if missing.
    return (
        sum(pv_err) / len(pv_err) if pv_err else None,
        sum(load_err) / len(load_err) if load_err else None,
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)
