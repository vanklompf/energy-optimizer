from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from energy_optimizer.battery_control import (
    BatteryControlIntent,
    ControlDirection,
    ControllerState,
    PlanFlowSnapshot,
    clamp_intent_power,
    direction_from_controller_state,
    intent_from_plan_flows,
    is_allowed_transition,
    require_neutral_before_reversal,
)
from energy_optimizer.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "db": ":memory:",
        "battery_control_max_charge_kw": 8.8,
        "battery_control_max_discharge_kw": 9.6,
        "battery_control_max_power_step_kw": 0.5,
        "battery_control_deadband_kw": 0.12,
        "battery_control_grid_charge_enabled": True,
        "battery_export_enabled": True,
        "battery_control_supported_directions": ["FALLBACK", "IDLE", "CHARGE", "DISCHARGE"],
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _flows(**overrides: float) -> PlanFlowSnapshot:
    base = PlanFlowSnapshot(
        interval_start=dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC),
        dt_hours=0.25,
        pv_to_battery_kwh=0.0,
        grid_to_battery_kwh=0.0,
        battery_to_load_kwh=0.0,
        battery_to_grid_kwh=0.0,
        battery_to_ev_kwh=0.0,
        grid_import_kwh=0.0,
        grid_export_kwh=0.0,
        pv_to_grid_kwh=0.0,
    )
    return replace(base, **overrides)


def test_near_zero_flows_map_to_idle() -> None:
    intent = intent_from_plan_flows(
        _flows(pv_to_battery_kwh=0.01, battery_to_load_kwh=0.01),
        settings=_settings(),
        source_run_id=1,
        now=dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC),
        expiry=dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC),
    )
    assert intent.direction == ControlDirection.IDLE
    assert intent.requested_power_kw == 0.0
    assert intent.grid_charge is False
    assert intent.export is False


def test_grid_to_battery_maps_to_charge_when_allowed() -> None:
    intent = intent_from_plan_flows(
        _flows(grid_to_battery_kwh=1.0, grid_import_kwh=1.1),
        settings=_settings(battery_control_grid_charge_enabled=True),
        source_run_id=2,
        now=dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC),
        expiry=dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC),
    )
    assert intent.direction == ControlDirection.CHARGE
    assert intent.grid_charge is True
    assert intent.requested_power_kw == pytest.approx(4.0)  # 1.0 kWh / 0.25 h


def test_grid_to_battery_rejected_when_grid_charge_disabled() -> None:
    with pytest.raises(ValueError, match="grid.charge|grid_charge"):
        intent_from_plan_flows(
            _flows(grid_to_battery_kwh=1.0),
            settings=_settings(battery_control_grid_charge_enabled=False),
            source_run_id=2,
            now=dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC),
            expiry=dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC),
        )


def test_battery_to_grid_maps_to_discharge_export_when_allowed() -> None:
    intent = intent_from_plan_flows(
        _flows(battery_to_grid_kwh=0.5, grid_export_kwh=0.45),
        settings=_settings(battery_export_enabled=True),
        source_run_id=3,
        now=dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC),
        expiry=dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC),
    )
    assert intent.direction == ControlDirection.DISCHARGE
    assert intent.export is True
    assert intent.requested_power_kw == pytest.approx(2.0)


def test_pv_only_battery_charge_does_not_force_grid_charge() -> None:
    intent = intent_from_plan_flows(
        _flows(pv_to_battery_kwh=0.8),
        settings=_settings(),
        source_run_id=4,
        now=dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC),
        expiry=dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC),
    )
    assert intent.direction == ControlDirection.CHARGE
    assert intent.grid_charge is False
    assert intent.export is False


def test_battery_to_load_does_not_force_export() -> None:
    intent = intent_from_plan_flows(
        _flows(battery_to_load_kwh=0.4),
        settings=_settings(),
        source_run_id=5,
        now=dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC),
        expiry=dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC),
    )
    assert intent.direction == ControlDirection.DISCHARGE
    assert intent.export is False


def test_contradictory_charge_and_discharge_rejected() -> None:
    with pytest.raises(ValueError, match="contradict|simultaneous|charge.*discharge"):
        intent_from_plan_flows(
            _flows(pv_to_battery_kwh=0.5, battery_to_load_kwh=0.5),
            settings=_settings(),
            source_run_id=6,
            now=dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC),
            expiry=dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC),
        )


def test_contradictory_import_and_export_rejected() -> None:
    with pytest.raises(ValueError, match="contradict|simultaneous|import.*export"):
        intent_from_plan_flows(
            _flows(grid_import_kwh=0.3, grid_export_kwh=0.3, pv_to_battery_kwh=0.5),
            settings=_settings(),
            source_run_id=7,
            now=dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC),
            expiry=dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC),
        )


def test_power_clamped_to_configured_limits_and_ramp() -> None:
    settings = _settings(
        battery_control_max_charge_kw=2.0,
        battery_control_max_power_step_kw=0.5,
    )
    intent = BatteryControlIntent(
        source_run_id=1,
        interval_start=dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC),
        direction=ControlDirection.CHARGE,
        requested_power_kw=8.0,
        cutoff_soc_pct=98.0,
        expiry=dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC),
        grid_charge=True,
        export=False,
        expected_grid_direction="import",
        expected_grid_kw_min=0.0,
        expected_grid_kw_max=2.0,
        expected_financial_value_pln=0.1,
        reason_codes=["grid_charge_arbitrage"],
    )
    clamped = clamp_intent_power(intent, settings=settings, previous_power_kw=0.0)
    assert clamped.requested_power_kw == pytest.approx(0.5)


def test_direction_from_controller_state_treats_only_active_states_as_directional() -> None:
    assert direction_from_controller_state("ACTIVE_CHARGE") is ControlDirection.CHARGE
    assert direction_from_controller_state("ACTIVE_DISCHARGE") is ControlDirection.DISCHARGE
    for neutral in ("ARMED_IDLE", "DISARMED", "FALLBACK", "LOCKOUT", "PREFLIGHT", "", None):
        assert direction_from_controller_state(neutral) is ControlDirection.IDLE


def test_persisted_active_state_round_trips_into_reversal_detection() -> None:
    """The persisted state string is the only cross-cycle memory of direction."""
    previous = direction_from_controller_state(ControllerState.ACTIVE_CHARGE.value)
    assert require_neutral_before_reversal(previous, ControlDirection.DISCHARGE) is True
    assert require_neutral_before_reversal(previous, ControlDirection.CHARGE) is False


def test_expired_or_non_current_interval_rejected() -> None:
    flows = _flows(pv_to_battery_kwh=0.5)
    with pytest.raises(ValueError, match="expired|current|interval"):
        intent_from_plan_flows(
            flows,
            settings=_settings(),
            source_run_id=8,
            now=dt.datetime(2026, 8, 8, 12, 20, tzinfo=dt.UTC),
            expiry=dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC),
        )
    with pytest.raises(ValueError, match="expired|current|interval"):
        intent_from_plan_flows(
            flows,
            settings=_settings(),
            source_run_id=8,
            now=dt.datetime(2026, 8, 8, 11, 50, tzinfo=dt.UTC),
            expiry=dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC),
        )


@pytest.mark.parametrize(
    "current,target,allowed",
    [
        (ControllerState.DISARMED, ControllerState.PREFLIGHT, True),
        (ControllerState.PREFLIGHT, ControllerState.ARMED_IDLE, True),
        (ControllerState.ARMED_IDLE, ControllerState.ACTIVE_CHARGE, True),
        (ControllerState.ACTIVE_CHARGE, ControllerState.ACTIVE_DISCHARGE, False),
        (ControllerState.ACTIVE_CHARGE, ControllerState.ARMED_IDLE, True),
        (ControllerState.ARMED_IDLE, ControllerState.ACTIVE_DISCHARGE, True),
        (ControllerState.LOCKOUT, ControllerState.ARMED_IDLE, False),
        (ControllerState.LOCKOUT, ControllerState.DISARMED, True),
        (ControllerState.ACTIVE_DISCHARGE, ControllerState.FALLBACK, True),
        (ControllerState.FALLBACK, ControllerState.DISARMED, True),
    ],
)
def test_state_transition_table(
    current: ControllerState, target: ControllerState, allowed: bool
) -> None:
    assert is_allowed_transition(current, target) is allowed


def test_direction_reversal_requires_neutral_transition() -> None:
    assert (
        require_neutral_before_reversal(ControlDirection.CHARGE, ControlDirection.DISCHARGE)
        is True
    )
    assert (
        require_neutral_before_reversal(ControlDirection.DISCHARGE, ControlDirection.CHARGE)
        is True
    )
    assert require_neutral_before_reversal(ControlDirection.CHARGE, ControlDirection.IDLE) is False
    assert (
        require_neutral_before_reversal(ControlDirection.CHARGE, ControlDirection.CHARGE) is False
    )
    assert (
        require_neutral_before_reversal(ControlDirection.IDLE, ControlDirection.CHARGE) is False
    )
