"""Pure domain types for stationary-battery control intents and state transitions.

No Home Assistant I/O lives here. Callers translate optimiser plan flows into typed
intents, clamp power, and validate controller state transitions fail-closed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from enum import StrEnum

from .config import Settings

# Flows below this absolute kW threshold after conversion are treated as idle/noise.
_NEAR_ZERO_KW = 0.05


class ControlDirection(StrEnum):
    FALLBACK = "FALLBACK"
    IDLE = "IDLE"
    CHARGE = "CHARGE"
    DISCHARGE = "DISCHARGE"


class ControllerState(StrEnum):
    DISARMED = "DISARMED"
    PREFLIGHT = "PREFLIGHT"
    ARMED_IDLE = "ARMED_IDLE"
    ACTIVE_CHARGE = "ACTIVE_CHARGE"
    ACTIVE_DISCHARGE = "ACTIVE_DISCHARGE"
    FALLBACK = "FALLBACK"
    LOCKOUT = "LOCKOUT"


# Explicit transition table. Direction reversal must pass through ARMED_IDLE / FALLBACK
# (neutral), never ACTIVE_CHARGE <-> ACTIVE_DISCHARGE directly.
_ALLOWED_TRANSITIONS: dict[ControllerState, frozenset[ControllerState]] = {
    ControllerState.DISARMED: frozenset(
        {ControllerState.PREFLIGHT, ControllerState.FALLBACK, ControllerState.DISARMED}
    ),
    ControllerState.PREFLIGHT: frozenset(
        {
            ControllerState.ARMED_IDLE,
            ControllerState.DISARMED,
            ControllerState.LOCKOUT,
            ControllerState.FALLBACK,
            ControllerState.PREFLIGHT,
        }
    ),
    ControllerState.ARMED_IDLE: frozenset(
        {
            ControllerState.ACTIVE_CHARGE,
            ControllerState.ACTIVE_DISCHARGE,
            ControllerState.FALLBACK,
            ControllerState.DISARMED,
            ControllerState.LOCKOUT,
            ControllerState.PREFLIGHT,
            ControllerState.ARMED_IDLE,
        }
    ),
    ControllerState.ACTIVE_CHARGE: frozenset(
        {
            ControllerState.ACTIVE_CHARGE,
            ControllerState.ARMED_IDLE,
            ControllerState.FALLBACK,
            ControllerState.LOCKOUT,
        }
    ),
    ControllerState.ACTIVE_DISCHARGE: frozenset(
        {
            ControllerState.ACTIVE_DISCHARGE,
            ControllerState.ARMED_IDLE,
            ControllerState.FALLBACK,
            ControllerState.LOCKOUT,
        }
    ),
    ControllerState.FALLBACK: frozenset(
        {
            ControllerState.DISARMED,
            ControllerState.LOCKOUT,
            ControllerState.PREFLIGHT,
            ControllerState.ARMED_IDLE,
            ControllerState.FALLBACK,
        }
    ),
    ControllerState.LOCKOUT: frozenset({ControllerState.DISARMED, ControllerState.LOCKOUT}),
}


@dataclass(frozen=True, slots=True)
class PlanFlowSnapshot:
    """Minimal plan-interval flows needed to build a control intent."""

    interval_start: dt.datetime
    dt_hours: float
    pv_to_battery_kwh: float
    grid_to_battery_kwh: float
    battery_to_load_kwh: float
    battery_to_grid_kwh: float
    battery_to_ev_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    pv_to_grid_kwh: float = 0.0


@dataclass(frozen=True, slots=True)
class BatteryControlIntent:
    source_run_id: int | None
    interval_start: dt.datetime
    direction: ControlDirection
    requested_power_kw: float
    cutoff_soc_pct: float
    expiry: dt.datetime
    grid_charge: bool
    export: bool
    expected_grid_direction: str | None
    expected_grid_kw_min: float | None
    expected_grid_kw_max: float | None
    expected_financial_value_pln: float | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ControlAuthorization:
    allowed: bool
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    clamped_intent: BatteryControlIntent | None
    plan_ts: dt.datetime | None
    telemetry_ts: dt.datetime | None
    price_ts: dt.datetime | None


@dataclass(frozen=True, slots=True)
class ControlResult:
    command_id: str
    requested_state: ControllerState
    observed_state: ControllerState | None
    entity_readback: dict[str, str | float | None]
    physical_verified: bool
    retries: int
    latency_ms: float
    failure_reason: str | None
    lockout_reason: str | None


def is_allowed_transition(current: ControllerState, target: ControllerState) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, frozenset())


def require_neutral_before_reversal(
    current: ControlDirection, target: ControlDirection
) -> bool:
    """True when moving between charge and discharge requires a neutral dwell."""
    active = {ControlDirection.CHARGE, ControlDirection.DISCHARGE}
    return current in active and target in active and current != target


def clamp_intent_power(
    intent: BatteryControlIntent,
    *,
    settings: Settings,
    previous_power_kw: float,
) -> BatteryControlIntent:
    if intent.direction == ControlDirection.CHARGE:
        cap = settings.battery_control_max_charge_kw
    elif intent.direction == ControlDirection.DISCHARGE:
        cap = settings.battery_control_max_discharge_kw
    else:
        return replace(intent, requested_power_kw=0.0)

    stepped = min(
        abs(intent.requested_power_kw),
        abs(previous_power_kw) + settings.battery_control_max_power_step_kw,
        cap,
    )
    if stepped < settings.battery_control_deadband_kw:
        stepped = 0.0
    return replace(intent, requested_power_kw=stepped)


def intent_from_plan_flows(
    flows: PlanFlowSnapshot,
    *,
    settings: Settings,
    source_run_id: int | None,
    now: dt.datetime,
    expiry: dt.datetime,
    cutoff_soc_pct: float | None = None,
    expected_financial_value_pln: float | None = None,
    reason_codes: tuple[str, ...] = (),
) -> BatteryControlIntent:
    """Translate one plan interval's energy flows into a typed battery intent."""
    _reject_stale_or_non_current_interval(flows, now=now, expiry=expiry)

    if flows.dt_hours <= 0:
        raise ValueError("plan interval dt_hours must be positive")

    charge_kwh = flows.pv_to_battery_kwh + flows.grid_to_battery_kwh
    discharge_kwh = (
        flows.battery_to_load_kwh + flows.battery_to_grid_kwh + flows.battery_to_ev_kwh
    )
    charge_kw = charge_kwh / flows.dt_hours
    discharge_kw = discharge_kwh / flows.dt_hours

    if charge_kw > _NEAR_ZERO_KW and discharge_kw > _NEAR_ZERO_KW:
        raise ValueError("contradictory simultaneous charge and discharge flows")
    if (
        flows.grid_import_kwh > _NEAR_ZERO_KW * flows.dt_hours
        and flows.grid_export_kwh > _NEAR_ZERO_KW * flows.dt_hours
    ):
        raise ValueError("contradictory simultaneous import and export flows")

    cutoff = (
        settings.battery_control_max_soc_pct if cutoff_soc_pct is None else cutoff_soc_pct
    )

    if charge_kw <= _NEAR_ZERO_KW and discharge_kw <= _NEAR_ZERO_KW:
        return BatteryControlIntent(
            source_run_id=source_run_id,
            interval_start=flows.interval_start,
            direction=ControlDirection.IDLE,
            requested_power_kw=0.0,
            cutoff_soc_pct=cutoff,
            expiry=expiry,
            grid_charge=False,
            export=False,
            expected_grid_direction=None,
            expected_grid_kw_min=None,
            expected_grid_kw_max=None,
            expected_financial_value_pln=expected_financial_value_pln,
            reason_codes=reason_codes or ("idle",),
        )

    if charge_kw > _NEAR_ZERO_KW:
        grid_charge = flows.grid_to_battery_kwh > _NEAR_ZERO_KW * flows.dt_hours
        if grid_charge and not settings.battery_control_grid_charge_enabled:
            raise ValueError(
                "grid_charge flow requested but battery_control_grid_charge_enabled is false"
            )
        return BatteryControlIntent(
            source_run_id=source_run_id,
            interval_start=flows.interval_start,
            direction=ControlDirection.CHARGE,
            requested_power_kw=charge_kw,
            cutoff_soc_pct=cutoff,
            expiry=expiry,
            grid_charge=grid_charge,
            export=False,
            expected_grid_direction="import" if grid_charge else None,
            expected_grid_kw_min=0.0 if grid_charge else None,
            expected_grid_kw_max=(
                settings.battery_control_max_grid_import_kw if grid_charge else None
            ),
            expected_financial_value_pln=expected_financial_value_pln,
            reason_codes=reason_codes
            or (("grid_charge_arbitrage",) if grid_charge else ("self_consumption",)),
        )

    export = flows.battery_to_grid_kwh > _NEAR_ZERO_KW * flows.dt_hours
    if export and not settings.battery_export_enabled:
        raise ValueError("export flow requested but battery_export_enabled is false")
    return BatteryControlIntent(
        source_run_id=source_run_id,
        interval_start=flows.interval_start,
        direction=ControlDirection.DISCHARGE,
        requested_power_kw=discharge_kw,
        cutoff_soc_pct=settings.battery_control_min_soc_pct
        if cutoff_soc_pct is None
        else cutoff_soc_pct,
        expiry=expiry,
        grid_charge=False,
        export=export,
        expected_grid_direction="export" if export else None,
        expected_grid_kw_min=0.0 if export else None,
        expected_grid_kw_max=(
            settings.battery_control_max_grid_export_kw if export else None
        ),
        expected_financial_value_pln=expected_financial_value_pln,
        reason_codes=reason_codes
        or (("battery_export_arbitrage",) if export else ("self_consumption",)),
    )


def _reject_stale_or_non_current_interval(
    flows: PlanFlowSnapshot,
    *,
    now: dt.datetime,
    expiry: dt.datetime,
) -> None:
    if now >= expiry:
        raise ValueError("control intent interval is expired")
    interval_end = flows.interval_start + dt.timedelta(hours=flows.dt_hours)
    if not (flows.interval_start <= now < interval_end):
        raise ValueError("control intent interval is not the current plan interval")
