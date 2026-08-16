"""REST API routes consumed by the SPA (and usable from HA REST sensors as fallback)."""

from __future__ import annotations

import datetime as dt
import json
import math
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from ..config import BATTERY_CONTROL_EXPECTED_ARM_TOKEN, Settings
from ..control_store import (
    DEFAULT_SITE_KEY,
    ensure_controller_state,
)
from ..ev import EV_FULL_TARGET_SOC_PCT
from ..optimiser import IntervalInput, optimise
from ..safety import CONTROL_ENABLED
from ..shadow_observation import ShadowAction, TelemetrySample, observe_shadow_action
from ..simulator import (
    BatteryParams,
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
from ..telemetry_energy import complete_hourly_energy
from .schemas import BacktestRequest, BacktestResponse, PolicyResult

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
    gates_ok = (
        settings.mode == "control"
        and settings.battery_control_enabled
        and settings.battery_control_arm_token == BATTERY_CONTROL_EXPECTED_ARM_TOKEN
    )
    with store.session() as session:
        state = ensure_controller_state(session)
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
    lockout_active = bool(
        state.lockout_until is not None and _aware(state.lockout_until) > now
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

    return {
        "mode": settings.mode,
        "battery_control_enabled": settings.battery_control_enabled,
        "arm_token_configured": bool(settings.battery_control_arm_token),
        "arm_token_matches": (
            settings.battery_control_arm_token == BATTERY_CONTROL_EXPECTED_ARM_TOKEN
        ),
        "export_enabled": settings.battery_export_enabled,
        "number_register_ack_reliable": settings.battery_control_number_register_ack_reliable,
        "number_register_ack_evidence_id": (
            settings.battery_control_number_register_ack_evidence_id or None
        ),
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
        "control_enabled": CONTROL_ENABLED,
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
        start = _parse_dt(body.start)
        end = _parse_dt(body.end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid start/end") from exc

    try:
        series = _load_series(store, start, end)
    except IncompleteSeriesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not series:
        raise HTTPException(status_code=404, detail="no settled Pstryk meter data in range")

    battery = _battery_params(settings, body.battery_overrides)
    settled_start = _parse_dt(series[0].interval_start)
    settled_end = _parse_dt(series[-1].interval_start) + dt.timedelta(hours=1)
    try:
        soc_start = _soc_start_kwh(store, settled_start, settings)
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
    opt = optimise(intervals, soc_start, _optimiser_params(settings, body.battery_overrides))
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
        series = _load_series(store, start, end)
    except IncompleteSeriesError as exc:
        return {**empty, "data_status": str(exc)}
    if not series:
        return empty

    battery = _battery_params(settings, {})
    settled_start = _parse_dt(series[0].interval_start)
    settled_end = _parse_dt(series[-1].interval_start) + dt.timedelta(hours=1)
    try:
        soc_start = _soc_start_kwh(store, settled_start, settings)
    except IncompleteSeriesError as exc:
        return {**empty, "data_status": str(exc)}

    actual = value_actual(series, battery)
    actual_cost = actual.cost.net_cost_pln if actual.cost is not None else None

    opt = optimise(_series_to_intervals(series), soc_start, _optimiser_params(settings, {}))
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
        series = _load_series(store, start, now)
    except IncompleteSeriesError as exc:
        return {
            "now": _iso(now),
            "tz": settings.tz,
            "data_status": str(exc),
            "points": [],
        }
    if not series:
        return {"now": _iso(now), "tz": settings.tz, "data_status": "unavailable", "points": []}

    settled_start = _parse_dt(series[0].interval_start)
    try:
        soc_start = _soc_start_kwh(store, settled_start, settings)
    except IncompleteSeriesError as exc:
        return {
            "now": _iso(now),
            "tz": settings.tz,
            "data_status": str(exc),
            "points": [],
        }
    opt = optimise(_series_to_intervals(series), soc_start, _optimiser_params(settings, {}))
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
        "settled_end": (_parse_dt(series[-1].interval_start) + dt.timedelta(hours=1)).isoformat(),
        "optimiser_status": opt.status,
        "points": points,
    }


def _actual_soc_by_hour(store: Store, start: dt.datetime, end: dt.datetime) -> dict[str, float]:
    with store.session() as session:
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
    buckets: dict[dt.datetime, list[float]] = {}
    for row in telem:
        if row.soc_pct is None:
            continue
        hour = _aware(row.ts).replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(row.soc_pct)
    return {
        hour.isoformat(): round(sum(values) / len(values), 1) for hour, values in buckets.items()
    }


# --- serialisation helpers -------------------------------------------------
def _ev_dict(ev: EvTelemetry | None, settings: Settings) -> dict | None:
    if ev is None:
        return None
    return {
        "ts": _iso(ev.ts),
        "soc_pct": ev.soc_pct,
        "charging_status": ev.charging_status,
        "charging_active": ev.charging_active,
        "plugged_in": (
            ev.charging_status is not None and ev.charging_status != settings.ev_unplugged_status
        ),
        "switch_on": ev.switch_on,
        "power_kw": ev.power_kw,
        "fault": ev.fault,
        "stale": ev.stale,
    }


def _ev_control_dict(
    control: EvControlStatus | None, settings: Settings, *, override_active: bool = False
) -> dict:
    return {
        "enabled": settings.ev_control_enabled,
        "target_soc_pct": EV_FULL_TARGET_SOC_PCT,
        "override_active": override_active,
        "effective_target_soc_pct": EV_FULL_TARGET_SOC_PCT,
        "minimum_target_soc_pct": settings.ev_minimum_target_soc_pct,
        "departure_hour": settings.ev_departure_hour,
        "relay_settle_seconds": settings.ev_relay_settle_seconds,
        "relay_verify_interval_seconds": settings.ev_relay_verify_interval_seconds,
        "relay_verify_timeout_seconds": settings.ev_relay_verify_timeout_seconds,
        "relay_failure_backoff_minutes": settings.ev_relay_failure_backoff_minutes,
        "opportunistic_grid_allowed": False,
        "forecast_surplus_factor": settings.ev_forecast_surplus_factor,
        "battery_reserve_pct": settings.battery_soc_min_pct,
        "battery_hard_floor_pct": settings.battery_hard_soc_min_pct,
        "battery_full_target_pct": settings.ev_battery_full_soc_pct,
        "policy_explanation": (
            "After the guaranteed departure minimum, the Mercedes is prioritised over "
            "charging the stationary battery. Forecast-backed charging may start before "
            "live PV surplus arrives only when the same forecast still fills the house "
            f"battery to {settings.ev_battery_full_soc_pct:.0f}% later. Guaranteed Mercedes "
            "departure charging may consume the battery reserve down to the hard floor; "
            "ordinary household use, opportunistic EV charging, and economic export preserve "
            "the reserve. Normal opportunistic charging never uses grid energy."
        ),
        "ts": _iso(control.ts) if control else None,
        "desired_on": control.desired_on if control else False,
        "planned_on": control.planned_on if control else None,
        "action": control.action if control else "none",
        "reason": control.reason if control else "no control decision yet",
    }


def _billing_meter_dict(row: PstrykMeterInterval | None) -> dict | None:
    if row is None:
        return None
    return {
        "source": "pstryk",
        "interval_start": _iso(row.interval_start),
        "interval_end": _iso(row.interval_end),
        "import_kwh": row.import_kwh,
        "export_kwh": row.export_kwh,
        "balance_kwh": row.balance_kwh,
        "settled": True,
        "fetched_at": _iso(row.fetched_at),
    }


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


def _plan_step_dict(s: PlanStep, ev: EvPlanStep | None = None) -> dict:
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
        "ev_charge_kwh": ev.charge_kwh if ev else 0.0,
        "ev_planned_on": ev.planned_on if ev else False,
    }


def _policy_result(name: str, cost) -> PolicyResult:  # noqa: ANN001
    return PolicyResult(
        policy=name,
        net_cost_pln=round(cost.net_cost_pln, 4),
        import_kwh=round(cost.import_kwh, 4),
        export_kwh=round(cost.export_kwh, 4),
        battery_throughput_kwh=round(cost.battery_throughput_kwh, 4),
    )


class IncompleteSeriesError(ValueError):
    """Settled Pstryk data cannot be compared without complete aligned inputs."""


def _load_series(store: Store, start: dt.datetime, end: dt.datetime) -> list[SeriesInterval]:
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
    expected_start = _ceil_hour(start)
    expected_end = _aware(end).replace(minute=0, second=0, microsecond=0)
    first_start = _aware(meter_rows[0].interval_start)
    last_end = _aware(meter_rows[-1].interval_end)
    if first_start != expected_start:
        raise IncompleteSeriesError(f"missing leading settlement before {first_start.isoformat()}")
    if last_end != expected_end:
        raise IncompleteSeriesError(f"missing trailing settlement after {last_end.isoformat()}")

    energy_by_hour = complete_hourly_energy(telem)
    price_by_hour = {
        _aware(row.interval_start).replace(minute=0, second=0, microsecond=0): row
        for row in prices
        if row.buy_gross is not None
    }
    series: list[SeriesInterval] = []
    expected_hour: dt.datetime | None = None
    for billing in meter_rows:
        hour = _aware(billing.interval_start).replace(minute=0, second=0, microsecond=0)
        interval_end = _aware(billing.interval_end)
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


def _soc_start_kwh(store: Store, start: dt.datetime, settings: Settings) -> float:
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
    row = min(rows, key=lambda item: abs(_aware(item.ts) - start))
    assert row.soc_pct is not None
    return row.soc_pct / 100.0 * settings.battery_capacity_kwh


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return _aware(value).isoformat()


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _ceil_hour(value: dt.datetime) -> dt.datetime:
    aware = _aware(value)
    floor = aware.replace(minute=0, second=0, microsecond=0)
    return floor if aware == floor else floor + dt.timedelta(hours=1)


def _parse_dt(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
