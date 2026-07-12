"""REST API routes consumed by the SPA (and usable from HA REST sensors as fallback)."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from ..config import Settings
from ..optimiser import IntervalInput, optimise
from ..safety import CONTROL_ENABLED
from ..simulator import (
    BatteryParams,
    SeriesInterval,
    get_policy,
    simulate_policy,
    value_actual,
    value_optimiser_plan,
)
from ..store import DailyReport, PlanStep, Price, Run, Store, Telemetry
from .schemas import BacktestRequest, BacktestResponse, PolicyResult

router = APIRouter(prefix="/api", tags=["api"])


def _store(request: Request) -> Store:
    return request.app.state.store


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/status")
def get_status(request: Request) -> dict:
    store = _store(request)
    settings = _settings(request)
    now = dt.datetime.now(tz=dt.UTC)
    floor = now.replace(minute=0, second=0, microsecond=0)
    with store.session() as session:
        telem = session.execute(
            select(Telemetry).order_by(Telemetry.ts.desc()).limit(1)
        ).scalar_one_or_none()
        price = session.execute(
            select(Price).where(Price.interval_start == floor)
        ).scalar_one_or_none()
        last_run = session.execute(
            select(Run).order_by(Run.ts.desc()).limit(1)
        ).scalar_one_or_none()
    return {
        "mode": settings.mode,
        "control_enabled": CONTROL_ENABLED,
        "now": now.isoformat(),
        "telemetry": _telemetry_dict(telem),
        "current_price": _price_dict(price),
        "last_run": _run_dict(last_run),
    }


@router.get("/plan")
def get_plan(request: Request) -> dict:
    store = _store(request)
    with store.session() as session:
        last_run = session.execute(
            select(Run).order_by(Run.ts.desc()).limit(1)
        ).scalar_one_or_none()
        if last_run is None:
            return {"run": None, "steps": []}
        steps = (
            session.execute(
                select(PlanStep)
                .where(PlanStep.run_id == last_run.run_id)
                .order_by(PlanStep.interval_start)
            )
            .scalars()
            .all()
        )
        run_dict = _run_dict(last_run)
        step_dicts = [_plan_step_dict(s) for s in steps]
    return {"run": run_dict, "steps": step_dicts}


@router.get("/runs")
def get_runs(request: Request, date: str | None = None) -> dict:
    store = _store(request)
    with store.session() as session:
        stmt = select(Run).order_by(Run.ts.desc())
        if date:
            try:
                day = dt.date.fromisoformat(date)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid date") from exc
            start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)
            end = start + dt.timedelta(days=1)
            stmt = stmt.where(Run.ts >= start).where(Run.ts < end)
        rows = session.execute(stmt.limit(500)).scalars().all()
        runs = [_run_dict(r) for r in rows]
    return {"runs": runs}


@router.get("/reports/daily")
def get_daily_reports(request: Request) -> dict:
    store = _store(request)
    with store.session() as session:
        rows = (
            session.execute(select(DailyReport).order_by(DailyReport.date.desc()).limit(365))
            .scalars()
            .all()
        )
        reports = [
            {c.name: getattr(r, c.name) for c in DailyReport.__table__.columns} for r in rows
        ]
    return {"reports": reports}


@router.post("/backtest", response_model=BacktestResponse)
def post_backtest(request: Request, body: BacktestRequest) -> BacktestResponse:
    store = _store(request)
    settings = _settings(request)
    try:
        start = _parse_dt(body.start)
        end = _parse_dt(body.end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid start/end") from exc

    series = _load_series(store, start, end)
    if not series:
        raise HTTPException(status_code=404, detail="no data in range")

    battery = _battery_params(settings, body.battery_overrides)
    soc_start = _soc_start_kwh(store, start, settings)

    results: list[PolicyResult] = []

    # Actual (measured) valuation, if telemetry has flows.
    actual = value_actual(series, battery)
    if actual.cost is not None:
        results.append(_policy_result("actual_sigen", actual.cost))

    for name in body.policies:
        try:
            policy = get_policy(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        sim = simulate_policy(series, policy, soc_start, battery)
        if sim.cost is not None:
            results.append(_policy_result(name, sim.cost))

    # Optimiser plan valuation over the same series.
    intervals = [
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
    opt = optimise(intervals, soc_start, _optimiser_params(settings, body.battery_overrides))
    if opt.status == "optimal":
        cost = value_optimiser_plan(opt, series, battery)
        results.append(_policy_result("optimiser", cost))

    return BacktestResponse(
        start=body.start, end=body.end, intervals=len(series), results=results
    )


# --- serialisation helpers -------------------------------------------------
def _telemetry_dict(t: Telemetry | None) -> dict | None:
    if t is None:
        return None
    return {
        "ts": _iso(t.ts),
        "soc_pct": t.soc_pct,
        "batt_charge_kw": t.batt_charge_kw,
        "batt_discharge_kw": t.batt_discharge_kw,
        "pv_kw": t.pv_kw,
        "load_kw": t.load_kw,
        "grid_import_kw": t.grid_import_kw,
        "grid_export_kw": t.grid_export_kw,
        "ems_mode": t.ems_mode,
        "stale": t.stale,
    }


def _price_dict(p: Price | None) -> dict | None:
    if p is None:
        return None
    return {
        "interval_start": _iso(p.interval_start),
        "buy_gross": p.buy_gross,
        "sell_gross": p.sell_gross,
        "full_price": p.full_price,
        "is_cheap": p.is_cheap,
        "is_expensive": p.is_expensive,
        "source": p.source,
    }


def _run_dict(r: Run | None) -> dict | None:
    if r is None:
        return None
    return {
        "run_id": r.run_id,
        "ts": _iso(r.ts),
        "mode": r.mode,
        "status": r.status,
        "reason": r.reason,
        "objective_pln": r.objective_pln,
        "horizon_hours": r.horizon_hours,
        "known_price_hours": r.known_price_hours,
        "solver_input_sha256": r.solver_input_sha256,
        "solve_ms": r.solve_ms,
    }


def _plan_step_dict(s: PlanStep) -> dict:
    return {
        "interval_start": _iso(s.interval_start),
        "dt_hours": s.dt_hours,
        "pv_to_load_kwh": s.pv_to_load_kwh,
        "pv_to_battery_kwh": s.pv_to_battery_kwh,
        "pv_to_grid_kwh": s.pv_to_grid_kwh,
        "grid_to_load_kwh": s.grid_to_load_kwh,
        "grid_to_battery_kwh": s.grid_to_battery_kwh,
        "battery_to_load_kwh": s.battery_to_load_kwh,
        "battery_to_grid_kwh": s.battery_to_grid_kwh,
        "curtail_kwh": s.curtail_kwh,
        "soc_pct_end": s.soc_pct_end,
        "marginal_value": s.marginal_value,
    }


def _policy_result(name: str, cost) -> PolicyResult:  # noqa: ANN001
    return PolicyResult(
        policy=name,
        net_cost_pln=round(cost.net_cost_pln, 4),
        import_kwh=round(cost.import_kwh, 4),
        export_kwh=round(cost.export_kwh, 4),
        battery_throughput_kwh=round(cost.battery_throughput_kwh, 4),
    )


def _load_series(store: Store, start: dt.datetime, end: dt.datetime) -> list[SeriesInterval]:
    """Build an hourly series from stored prices + telemetry over [start, end)."""
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
    # Aggregate telemetry to hourly energy (kWh) using average power * 1h per hour bucket.
    by_hour = _hourly_telemetry(telem)
    series: list[SeriesInterval] = []
    for p in prices:
        if p.buy_gross is None:
            continue
        hour = _aware(p.interval_start).replace(minute=0, second=0, microsecond=0)
        agg = by_hour.get(hour, {})
        series.append(
            SeriesInterval(
                interval_start=hour.isoformat(),
                dt_hours=1.0,
                pv_energy_kwh=agg.get("pv", 0.0),
                load_energy_kwh=agg.get("load", 0.0),
                buy_price=float(p.buy_gross),
                sell_price=float(p.sell_gross or 0.0),
                measured_grid_import_kwh=agg.get("grid_import", 0.0),
                measured_grid_export_kwh=agg.get("grid_export", 0.0),
                measured_charge_kwh=agg.get("charge", 0.0),
                measured_discharge_kwh=agg.get("discharge", 0.0),
            )
        )
    return series


def _hourly_telemetry(telem: list[Telemetry]) -> dict[dt.datetime, dict[str, float]]:
    buckets: dict[dt.datetime, dict[str, list[float]]] = {}
    for t in telem:
        hour = _aware(t.ts).replace(minute=0, second=0, microsecond=0)
        b = buckets.setdefault(hour, {})
        for key, val in (
            ("pv", t.pv_kw),
            ("load", t.load_kw),
            ("grid_import", t.grid_import_kw),
            ("grid_export", t.grid_export_kw),
            ("charge", t.batt_charge_kw),
            ("discharge", t.batt_discharge_kw),
        ):
            if val is not None:
                b.setdefault(key, []).append(val)
    out: dict[dt.datetime, dict[str, float]] = {}
    for hour, series in buckets.items():
        out[hour] = {k: (sum(v) / len(v)) for k, v in series.items()}  # mean kW ~ kWh for 1h
    return out


def _battery_params(settings: Settings, overrides: dict[str, float]) -> BatteryParams:
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


def _optimiser_params(settings: Settings, overrides: dict[str, float]):  # noqa: ANN201
    from ..optimiser import OptimiserParams

    return OptimiserParams(
        battery_capacity_kwh=overrides.get("capacity_kwh", settings.battery_capacity_kwh),
        soc_min_kwh=overrides.get("soc_min_kwh", settings.soc_min_kwh),
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
    )


def _soc_start_kwh(store: Store, start: dt.datetime, settings: Settings) -> float:
    with store.session() as session:
        row = session.execute(
            select(Telemetry).where(Telemetry.ts >= start).order_by(Telemetry.ts).limit(1)
        ).scalar_one_or_none()
    if row and row.soc_pct is not None:
        return row.soc_pct / 100.0 * settings.battery_capacity_kwh
    return settings.soc_min_kwh


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return _aware(value).isoformat()


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _parse_dt(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
