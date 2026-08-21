"""REST API routes consumed by the SPA (and usable from HA REST sensors as fallback)."""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from ..config import Settings
from ..control_store import (
    DEFAULT_SITE_KEY,
    clear_lockout,
    ensure_controller_state,
    expire_lockout_if_due,
    is_locked_out,
)
from ..optimiser import IntervalInput, optimise
from ..reports import (
    IncompleteSeriesError,
    battery_params,
    load_settled_series,
    optimiser_params,
    parse_dt,
    soc_start_kwh,
)
from ..shadow_observation import ShadowAction, TelemetrySample, observe_shadow_action
from ..simulator import (
    SeriesInterval,
    get_policy,
    simulate_policy,
    value_actual,
    value_optimiser_plan,
)
from ..store import (
    ControlAction,
    ControllerLease,
    DailyReport,
    EvControlStatus,
    EvPlanStep,
    EvTelemetry,
    PlanStep,
    Price,
    PstrykMeterInterval,
    Run,
    Store,
    Telemetry,
)
from .schemas import BacktestRequest, BacktestResponse, PolicyResult
from .serializers import (
    _actual_soc_by_hour,
    _aware,
    _billing_meter_dict,
    _ev_control_dict,
    _ev_dict,
    _iso,
    _plan_step_dict,
    _policy_result,
    _price_dict,
    _run_dict,
    _telemetry_dict,
)

router = APIRouter(prefix="/api", tags=["api"])


def _store(request: Request) -> Store:
    return request.app.state.store


def _settings(request: Request) -> Settings:
    return request.app.state.settings


async def _battery_control_status(request: Request, now: dt.datetime) -> dict:
    """Read-only battery controller observability. Never exposes arm tokens or secrets."""
    store = _store(request)
    settings = _settings(request)
    service = request.app.state.service
    gates_ok = settings.battery_actuation_live
    with store.session() as session:
        expire_lockout_if_due(session, now=now)
        state = ensure_controller_state(session)
        lockout_active = is_locked_out(session, now=now)
        lease = session.get(ControllerLease, DEFAULT_SITE_KEY)
        last_action = session.execute(
            select(ControlAction).order_by(ControlAction.created_at.desc()).limit(1)
        ).scalar_one_or_none()
        last_verified = session.execute(
            select(ControlAction)
            .where(ControlAction.result == "ok")
            .order_by(ControlAction.updated_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    lease_expires = _aware(lease.expires_at) if lease is not None else None
    lease_held = bool(
        lease is not None
        and lease.owner_id == getattr(service, "controller_owner_id", None)
        and lease_expires is not None
        and lease_expires > now
    )
    heartbeat_at = state.last_heartbeat_at
    heartbeat_age = (
        max(0.0, (now - _aware(heartbeat_at)).total_seconds()) if heartbeat_at else None
    )
    effective = "DISARMED"
    if lockout_active:
        effective = "LOCKOUT"
    elif settings.mode != "control" or not settings.battery_control_enabled:
        effective = "DRY_RUN"
    elif gates_ok:
        effective = state.state

    intent = None
    if last_action is not None and last_action.intent_json:
        try:
            import json

            intent = json.loads(last_action.intent_json)
        except json.JSONDecodeError:
            intent = {"raw": last_action.intent_json}

    physical = None
    if last_action is not None and last_action.physical_json:
        try:
            import json

            physical = json.loads(last_action.physical_json)
        except json.JSONDecodeError:
            physical = None

    watchdog_healthy, watchdog_reason = await service._watchdog_health(now)

    manual_command = None
    manual_command_status = getattr(service, "manual_command_status", None)
    if callable(manual_command_status):
        manual_command = manual_command_status(now)
    # Charge-only view kept for backward compatibility with existing clients.
    manual_charge = (
        manual_command if manual_command and manual_command.get("direction") == "CHARGE" else None
    )

    return {
        "mode": settings.mode,
        "battery_control_enabled": settings.battery_control_enabled,
        "export_enabled": settings.battery_export_enabled,
        "grid_charge_enabled": settings.battery_control_grid_charge_enabled,
        "discharge_enabled": settings.battery_control_authorize_discharge,
        "gates_ok": gates_ok,
        "effective_state": effective,
        "controller_state": state.state,
        "lease": {
            "held": lease_held,
            "owner_id": lease.owner_id if lease else None,
            "expires_at": lease_expires.isoformat() if lease_expires else None,
            "self_owner_id": getattr(service, "controller_owner_id", None),
        },
        "watchdog_healthy": watchdog_healthy,
        "watchdog_reason": watchdog_reason,
        "heartbeat_age_seconds": heartbeat_age,
        "heartbeat_expiry_seconds": settings.battery_control_heartbeat_expiry_seconds,
        "current_intent": intent,
        "manual_command": manual_command,
        "manual_charge": manual_charge,
        "last_action": _control_action_dict(last_action),
        "last_verified_action": _control_action_dict(last_verified),
        "last_fallback_at": (
            _aware(state.last_fallback_at).isoformat() if state.last_fallback_at else None
        ),
        "last_fallback_verified": bool(state.last_fallback_verified),
        "lockout": {
            "active": lockout_active,
            "until": (
                _aware(state.lockout_until).isoformat() if state.lockout_until else None
            ),
            "reason": state.lockout_reason,
        },
        "physical_verification": physical,
    }


def _control_action_dict(action: ControlAction | None) -> dict | None:
    if action is None:
        return None
    blockers = None
    if action.blockers_json:
        try:
            import json

            blockers = json.loads(action.blockers_json)
        except json.JSONDecodeError:
            blockers = [action.blockers_json]
    return {
        "command_id": action.command_id,
        "created_at": _aware(action.created_at).isoformat(),
        "updated_at": _aware(action.updated_at).isoformat(),
        "source_run_id": action.source_run_id,
        "interval_start": (
            _aware(action.interval_start).isoformat() if action.interval_start else None
        ),
        "authorization_allowed": action.authorization_allowed,
        "blockers": blockers,
        "requested_state": action.requested_state,
        "observed_state": action.observed_state,
        "result": action.result,
        "error_code": action.error_code,
        "latency_ms": action.latency_ms,
    }


@router.get("/status")
async def get_status(request: Request) -> dict:
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
        ev = session.execute(
            select(EvTelemetry).order_by(EvTelemetry.ts.desc()).limit(1)
        ).scalar_one_or_none()
        ev_control = session.get(EvControlStatus, "current")
        billing_meter = session.execute(
            select(PstrykMeterInterval).order_by(PstrykMeterInterval.interval_start.desc()).limit(1)
        ).scalar_one_or_none()
    return {
        "mode": settings.mode,
        "control_enabled": settings.battery_actuation_live,
        "now": now.isoformat(),
        "telemetry": _telemetry_dict(telem),
        "current_price": _price_dict(price),
        "billing_meter": _billing_meter_dict(billing_meter),
        "last_run": _run_dict(last_run),
        "ev": _ev_dict(ev, settings),
        "ev_control": _ev_control_dict(
            ev_control,
            settings,
            override_active=request.app.state.service.ev_charge_to_100_active,
        ),
        "battery_control": await _battery_control_status(request, now),
    }


@router.get("/control/actions")
def get_control_actions(request: Request, limit: int = 20) -> dict:
    """Recent battery control audit history (read-only)."""
    store = _store(request)
    limit = max(1, min(limit, 200))
    with store.session() as session:
        rows = (
            session.execute(
                select(ControlAction).order_by(ControlAction.created_at.desc()).limit(limit)
            )
            .scalars()
            .all()
        )
    return {"actions": [_control_action_dict(row) for row in rows]}


class ActuationRequest(BaseModel):
    enabled: bool


@router.post("/control/actuation")
async def set_actuation(request: Request, body: ActuationRequest) -> dict:
    """Enable or disable battery actuation for this process (env still wins on restart)."""
    settings = _settings(request)
    service = request.app.state.service
    if not body.enabled:
        service.clear_manual_charge()
        # Fallback must run while actuation is still live; otherwise
        # fallback_battery records DISARMED and never writes the inverter.
        try:
            await service.fallback_battery("actuation_disabled")
        finally:
            settings.battery_control_enabled = False
    else:
        settings.battery_control_enabled = True
    return {
        "mode": settings.mode,
        "battery_control_enabled": settings.battery_control_enabled,
        "control_enabled": settings.battery_actuation_live,
    }


class ManualCommandBody(BaseModel):
    direction: str
    target_kw: float
    duration_seconds: float | None = None


class ManualChargeBody(BaseModel):
    target_kw: float
    duration_seconds: float | None = None


@router.post("/control/manual-command")
def arm_manual_command(request: Request, body: ManualCommandBody) -> dict:
    """Arm one battery command, overriding the direction the plan chose.

    ``direction`` is ``CHARGE``, ``DISCHARGE`` (to house load), or ``EXPORT`` (to grid).
    The request expires on its own and is dropped on restart. Every gate, blocker, ramp
    limit, and physical verification still applies to the resulting command.
    """
    service = request.app.state.service
    now = dt.datetime.now(tz=dt.UTC)
    try:
        service.request_manual_command(
            direction=body.direction,
            target_kw=body.target_kw,
            duration_seconds=body.duration_seconds,
            now=now,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"armed": True, "manual_command": service.manual_command_status(now)}


@router.delete("/control/manual-command")
def clear_manual_command(request: Request) -> dict:
    """Drop an armed request; the next control cycle returns to the plan."""
    service = request.app.state.service
    service.clear_manual_command()
    return {"armed": False, "manual_command": None}


@router.post("/control/manual-charge")
def arm_manual_charge(request: Request, body: ManualChargeBody) -> dict:
    """Charge-only alias for ``/control/manual-command`` (ROADMAP checkpoint 1)."""
    service = request.app.state.service
    now = dt.datetime.now(tz=dt.UTC)
    try:
        service.request_manual_charge(
            target_kw=body.target_kw,
            duration_seconds=body.duration_seconds,
            now=now,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"armed": True, "manual_charge": service.manual_command_status(now)}


@router.delete("/control/manual-charge")
def clear_manual_charge(request: Request) -> dict:
    """Drop an armed request; the next control cycle returns to the plan."""
    service = request.app.state.service
    service.clear_manual_command()
    return {"armed": False, "manual_charge": None}


@router.post("/control/lockout/clear")
def clear_control_lockout(request: Request) -> dict:
    """Manually clear an auto-recovering control backoff.

    This only lifts ``lockout_until``. It does not fallback the inverter or
    rewrite a live ``ACTIVE_CHARGE`` / ``ACTIVE_DISCHARGE`` state to DISARMED.
    """
    store = _store(request)
    now = dt.datetime.now(tz=dt.UTC)
    with store.session() as session:
        state = clear_lockout(session, now=now)
    return {
        "cleared": True,
        "state": state.state,
        "lockout_active": False,
    }


@router.get("/control/shadow-observations")
def get_shadow_observations(request: Request, limit: int = 96) -> dict:
    """Compare completed shadow intents with later passive Sigen telemetry.

    This endpoint is evidence for Stage B only. It never makes a Home Assistant
    request, sends an MQTT command, or treats a local-EMS match as actuation proof.
    """
    store = _store(request)
    settings = _settings(request)
    now = dt.datetime.now(tz=dt.UTC)
    limit = max(1, min(limit, 500))
    with store.session() as session:
        actions = (
            session.execute(
                select(ControlAction)
                .where(ControlAction.result == "shadow")
                .where(ControlAction.interval_start.is_not(None))
                .order_by(ControlAction.created_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        observations = []
        for action in actions:
            if action.interval_start is None:
                continue
            start = _aware(action.interval_start)
            end = start + dt.timedelta(hours=settings.step_hours)
            if end > now or not action.intent_json:
                continue
            try:
                intent = json.loads(action.intent_json)
            except json.JSONDecodeError:
                continue
            if not intent.get("shadow"):
                continue
            telemetry = (
                session.execute(
                    select(Telemetry)
                    .where(Telemetry.ts >= start)
                    .where(Telemetry.ts < end)
                    .where(Telemetry.stale.is_(False))
                    .order_by(Telemetry.ts)
                )
                .scalars()
                .all()
            )
            observation = observe_shadow_action(
                ShadowAction(
                    command_id=action.command_id,
                    interval_start=start,
                    dt_hours=settings.step_hours,
                    direction=str(intent.get("direction", "IDLE")),
                    requested_power_kw=float(intent.get("requested_power_kw", 0.0)),
                ),
                [
                    TelemetrySample(
                        ts=_aware(sample.ts),
                        batt_charge_kw=sample.batt_charge_kw,
                        batt_discharge_kw=sample.batt_discharge_kw,
                    )
                    for sample in telemetry
                ],
            )
            observations.append(
                {
                    "command_id": observation.command_id,
                    "source_run_id": action.source_run_id,
                    "interval_start": start.isoformat(),
                    "planned_direction": str(intent.get("direction", "IDLE")),
                    "requested_power_kw": float(intent.get("requested_power_kw", 0.0)),
                    "status": observation.status,
                    "actual_direction": observation.actual_direction,
                    "average_battery_kw": observation.average_battery_kw,
                    "sample_count": observation.sample_count,
                }
            )
    return {"observations": observations}


@router.get("/prices")
def get_prices(request: Request, past_hours: int = 12, future_hours: int = 24) -> dict:
    """Hourly prices in a window around now for the dashboard chart. Past hours are actual;
    future hours are day-ahead where published (source ``api``) or padded (``forecast``)."""
    store = _store(request)
    now = dt.datetime.now(tz=dt.UTC)
    floor = now.replace(minute=0, second=0, microsecond=0)
    past_hours = max(0, min(past_hours, 168))
    future_hours = max(0, min(future_hours, 168))
    start = floor - dt.timedelta(hours=past_hours)
    end = floor + dt.timedelta(hours=future_hours + 1)
    with store.session() as session:
        rows = (
            session.execute(
                select(Price)
                .where(Price.interval_start >= start)
                .where(Price.interval_start < end)
                .order_by(Price.interval_start)
            )
            .scalars()
            .all()
        )
    prices = [_price_dict(p) for p in rows if p.buy_gross is not None]
    return {"now": now.isoformat(), "current_hour": _iso(floor), "prices": prices}


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
        ev_steps = (
            session.execute(select(EvPlanStep).where(EvPlanStep.run_id == last_run.run_id))
            .scalars()
            .all()
        )
        ev_by_start = {_aware(step.interval_start): step for step in ev_steps}
        run_dict = _run_dict(last_run)
        step_dicts = [
            _plan_step_dict(step, ev_by_start.get(_aware(step.interval_start))) for step in steps
        ]
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
        start = parse_dt(body.start)
        end = parse_dt(body.end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid start/end") from exc

    try:
        series = load_settled_series(store, start, end)
    except IncompleteSeriesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not series:
        raise HTTPException(status_code=404, detail="no settled Pstryk meter data in range")

    battery = battery_params(settings, body.battery_overrides)
    settled_start = parse_dt(series[0].interval_start)
    settled_end = parse_dt(series[-1].interval_start) + dt.timedelta(hours=1)
    try:
        soc_start = soc_start_kwh(store, settled_start, settings)
    except IncompleteSeriesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    results: list[PolicyResult] = []

    # Actual valuation is exclusively the Pstryk settlement meter. Unsettled/missing
    # hours are omitted rather than silently replaced with inverter telemetry.
    actual = value_actual(series, battery)
    if actual.cost is not None:
        results.append(_policy_result("actual_pstryk", actual.cost))

    for name in body.policies:
        try:
            policy = get_policy(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        sim = simulate_policy(series, policy, soc_start, battery)
        if sim.cost is not None:
            results.append(_policy_result(name, sim.cost))

    # Optimiser plan valuation over the same series.
    intervals = _series_to_intervals(series)
    opt = optimise(intervals, soc_start, optimiser_params(settings, body.battery_overrides))
    if opt.status == "optimal":
        cost = value_optimiser_plan(opt, series, battery)
        results.append(_policy_result("optimiser", cost))

    return BacktestResponse(
        start=body.start,
        end=body.end,
        settled_start=settled_start.isoformat(),
        settled_end=settled_end.isoformat(),
        intervals=len(series),
        results=results,
    )


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


def _window_savings(store: Store, settings: Settings, start: dt.datetime, end: dt.datetime) -> dict:
    """Realised savings over a *past* window: measured actual cost minus the
    perfect-foresight optimiser cost over the same recorded PV/load/prices."""
    empty = {
        "actual_cost_pln": None,
        "optimiser_cost_pln": None,
        "savings_pln": None,
        "intervals": 0,
        "settled_start": None,
        "settled_end": None,
        "data_status": "unavailable",
    }
    try:
        series = load_settled_series(store, start, end)
    except IncompleteSeriesError as exc:
        return {**empty, "data_status": str(exc)}
    if not series:
        return empty

    battery = battery_params(settings, {})
    settled_start = parse_dt(series[0].interval_start)
    settled_end = parse_dt(series[-1].interval_start) + dt.timedelta(hours=1)
    try:
        soc_start = soc_start_kwh(store, settled_start, settings)
    except IncompleteSeriesError as exc:
        return {**empty, "data_status": str(exc)}

    actual = value_actual(series, battery)
    actual_cost = actual.cost.net_cost_pln if actual.cost is not None else None

    opt = optimise(_series_to_intervals(series), soc_start, optimiser_params(settings, {}))
    opt_cost = (
        value_optimiser_plan(opt, series, battery).net_cost_pln if opt.status == "optimal" else None
    )

    savings = actual_cost - opt_cost if actual_cost is not None and opt_cost is not None else None
    return {
        "actual_cost_pln": round(actual_cost, 4) if actual_cost is not None else None,
        "optimiser_cost_pln": round(opt_cost, 4) if opt_cost is not None else None,
        "savings_pln": round(savings, 4) if savings is not None else None,
        "intervals": len(series),
        "settled_start": settled_start.isoformat(),
        "settled_end": settled_end.isoformat(),
        "data_status": "complete",
    }


@router.get("/savings")
def get_savings(request: Request) -> dict:
    """Past (realised) savings for today and the trailing 7 days."""
    store = _store(request)
    settings = _settings(request)
    now = dt.datetime.now(tz=dt.UTC)
    local_now = now.astimezone(ZoneInfo(settings.tz))
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.UTC)
    week_start = day_start - dt.timedelta(days=6)
    return {
        "now": _iso(now),
        "day": _window_savings(store, settings, day_start, now),
        "week": _window_savings(store, settings, week_start, now),
    }


@router.get("/comparison/hourly")
def get_hourly_comparison(request: Request, hours: int = 48) -> dict:
    """Hour-by-hour comparison of what actually happened vs what the perfect-foresight
    optimiser would have done over the same recorded PV/load/prices. Focused on battery
    charge/discharge timing rather than aggregate savings."""
    store = _store(request)
    settings = _settings(request)
    hours = max(1, min(hours, 720))
    now = dt.datetime.now(tz=dt.UTC)
    floor = now.replace(minute=0, second=0, microsecond=0)
    start = floor - dt.timedelta(hours=hours)

    try:
        series = load_settled_series(store, start, now)
    except IncompleteSeriesError as exc:
        return {
            "now": _iso(now),
            "tz": settings.tz,
            "data_status": str(exc),
            "points": [],
        }
    if not series:
        return {"now": _iso(now), "tz": settings.tz, "data_status": "unavailable", "points": []}

    settled_start = parse_dt(series[0].interval_start)
    try:
        soc_start = soc_start_kwh(store, settled_start, settings)
    except IncompleteSeriesError as exc:
        return {
            "now": _iso(now),
            "tz": settings.tz,
            "data_status": str(exc),
            "points": [],
        }
    opt = optimise(_series_to_intervals(series), soc_start, optimiser_params(settings, {}))
    opt_by_start = {s.interval_start: s for s in opt.steps} if opt.status == "optimal" else {}

    # Actual hourly SoC (mean of telemetry within the hour), keyed by hour ISO.
    soc_by_hour = _actual_soc_by_hour(store, start, now)

    points: list[dict] = []
    for s in series:
        step = opt_by_start.get(s.interval_start)
        actual_charge = s.measured_charge_kwh or 0.0
        actual_discharge = s.measured_discharge_kwh or 0.0
        opt_charge = step.pv_to_battery_kwh + step.grid_to_battery_kwh if step else None
        opt_discharge = step.battery_to_load_kwh + step.battery_to_grid_kwh if step else None
        points.append(
            {
                "interval_start": s.interval_start,
                "buy_price": round(s.buy_price, 4),
                "sell_price": round(s.sell_price, 4),
                "actual_charge_kwh": round(actual_charge, 3),
                "actual_discharge_kwh": round(actual_discharge, 3),
                "optimiser_charge_kwh": (round(opt_charge, 3) if opt_charge is not None else None),
                "optimiser_discharge_kwh": (
                    round(opt_discharge, 3) if opt_discharge is not None else None
                ),
                "actual_soc_pct": soc_by_hour.get(s.interval_start),
                "optimiser_soc_pct": round(step.soc_pct_end, 1) if step else None,
            }
        )
    return {
        "now": _iso(now),
        "tz": settings.tz,
        "data_status": "complete",
        "settled_start": series[0].interval_start,
        "settled_end": (parse_dt(series[-1].interval_start) + dt.timedelta(hours=1)).isoformat(),
        "optimiser_status": opt.status,
        "points": points,
    }


