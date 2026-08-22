"""JSON serialisers for the REST API."""

from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import select

from ..config import Settings
from ..ev import EV_FULL_TARGET_SOC_PCT
from ..store import (
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
from .schemas import PolicyResult


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
    safety = None
    if r.safety:
        try:
            safety = json.loads(r.safety)
        except json.JSONDecodeError:
            safety = {"raw": r.safety}
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
        "safety": safety,
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


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return _aware(value).isoformat()


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)

