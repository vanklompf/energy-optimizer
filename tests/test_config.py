from __future__ import annotations

import math

import pytest

from energy_optimizer.config import PvPlane, Settings


def test_derived_efficiencies_and_soc(monkeypatch) -> None:
    monkeypatch.setenv("EO_BATTERY_ROUND_TRIP_EFFICIENCY", "0.9")
    monkeypatch.setenv("EO_BATTERY_CAPACITY_KWH", "18.08")
    monkeypatch.setenv("EO_BATTERY_SOC_MIN_PCT", "20")
    monkeypatch.setenv("EO_BATTERY_SOC_MAX_PCT", "98")
    s = Settings(db=":memory:")
    assert abs(s.eta_charge - math.sqrt(0.9)) < 1e-9
    assert abs(s.eta_discharge - math.sqrt(0.9)) < 1e-9
    assert abs(s.soc_min_kwh - 18.08 * 0.20) < 1e-9
    assert abs(s.hard_soc_min_kwh - 0.0) < 1e-9
    assert abs(s.soc_max_kwh - 18.08 * 0.98) < 1e-9


def test_step_hours() -> None:
    s = Settings(db=":memory:", step_minutes=15)
    assert s.step_hours == 0.25


def test_price_window_bound_never_discards_published_prices() -> None:
    # The bound is a fetch window, not the planning horizon. Pstryk peaks at roughly 34h
    # of forward coverage just after the day-ahead publication, so the bound must stay
    # above that or real published prices would be thrown away.
    s = Settings(db=":memory:")
    assert s.optimise_horizon_hours >= 36
    assert 0 < s.optimise_min_price_hours <= 10


def test_battery_soc_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="hard.*reserve.*maximum"):
        Settings(
            db=":memory:",
            battery_hard_soc_min_pct=20,
            battery_soc_min_pct=15,
            battery_soc_max_pct=98,
        )


def test_pv_planes_from_json_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "EO_PV_PLANES",
        '[{"peak_kwp":4.0,"tilt":30,"azimuth":-45},{"peak_kwp":3.0,"tilt":30,"azimuth":45}]',
    )
    s = Settings(db=":memory:")
    assert len(s.pv_planes) == 2
    assert isinstance(s.pv_planes[0], PvPlane)
    assert s.pv_planes[0].peak_kwp == 4.0
    assert s.pv_planes[1].azimuth == 45


def test_ev_control_settings_have_safe_homelab_defaults() -> None:
    s = Settings(db=":memory:")
    assert s.ev_control_enabled is False
    assert s.ev_switch_entity == "switch.ev_charger"
    assert s.ev_soc_entity == "sensor.ev_state_of_charge"
    assert s.ev_charging_status_entity == "sensor.ev_charging_status"
    assert s.ev_charge_to_100_entity == "input_boolean.ev_charge_to_100_once"
    assert s.ev_unplugged_status == "3"
    assert s.ev_start_charging_statuses == ["2"]
    assert "0" in s.ev_active_charging_statuses
    assert "16" not in s.ev_active_charging_statuses
    assert s.ev_capacity_kwh == 10.9
    assert s.ev_charge_power_kw == 1.8
    assert s.ev_minimum_target_soc_pct == 50.0
    assert s.ev_departure_hour == 9
    assert s.ev_min_on_minutes == 5
    assert s.ev_min_off_minutes == 5
    assert s.ev_power_start_grace_minutes == 5
    assert s.ev_min_charging_power_kw == 0.1
    assert s.ev_relay_settle_seconds == 5.0
    assert s.ev_relay_verify_interval_seconds == 2.0
    assert s.ev_relay_verify_timeout_seconds == 30.0
    assert s.ev_relay_failure_backoff_minutes == 30
    assert s.ev_forecast_surplus_factor == 0.8


def test_battery_control_defaults_are_non_actuating() -> None:
    s = Settings(db=":memory:")
    assert s.mode == "dry_run"
    assert s.battery_control_enabled is False
    assert s.battery_actuation_live is False
    assert s.battery_export_enabled is False
    assert s.battery_control_grid_charge_enabled is False
    assert s.battery_control_authorize_discharge is False
    assert "DISCHARGE" not in s.battery_control_supported_directions
    assert s.battery_control_remote_ems_switch_entity == (
        "switch.sigen_plant_remote_ems_controlled_by_home_assistant"
    )
    assert s.battery_control_mode_select_entity == ("select.sigen_plant_remote_ems_control_mode")
    assert s.battery_control_mode_standby == "Standby"
    assert s.battery_control_mode_charge_grid_first == "Command Charging (Grid First)"
    assert s.battery_control_discharge_command_mode == "Command Discharging (PV First)"
    assert s.battery_control_export_command_mode == "Command Discharging (ESS First)"
    assert s.battery_control_export_pv_command_mode == "Command Discharging (PV First)"
    assert s.terminal_soc_salvage_auto is True
    assert s.battery_control_fallback_mode == "Standby"
    assert s.battery_control_local_charge_limit_kw == 8.8
    assert s.battery_control_local_discharge_limit_kw == 9.6
    assert s.battery_control_local_charge_cutoff_pct == 100.0
    assert s.battery_control_local_discharge_cutoff_pct == 0.0
    assert s.battery_control_standby_neutral_band_kw == 0.12
    assert s.battery_control_physical_verify_timeout_seconds >= 15.0
    # Planner feasibility flags must stay independent of physical actuation gates.
    assert s.allow_grid_charging is True
    assert s.allow_battery_export is True


def _armed_control_kwargs(**overrides: object) -> dict[str, object]:
    """Minimal kwargs that satisfy control-mode startup validation."""
    base: dict[str, object] = {
        "db": ":memory:",
        "mode": "control",
        "battery_control_enabled": True,
        "battery_control_grid_charge_enabled": True,
        "battery_control_supported_directions": ["FALLBACK", "IDLE", "CHARGE"],
    }
    base.update(overrides)
    return base


def test_control_mode_requires_enabled_gate() -> None:
    Settings(**_armed_control_kwargs())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="battery_control_enabled"):
        Settings(**_armed_control_kwargs(battery_control_enabled=False))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("battery_control_remote_ems_switch_entity", "", "entity"),
        ("battery_control_mode_select_entity", "", "entity"),
        ("battery_control_charge_limit_entity", "", "entity"),
        ("battery_control_discharge_limit_entity", "", "entity"),
        ("battery_control_charge_cutoff_entity", "", "entity"),
        ("battery_control_discharge_cutoff_entity", "", "entity"),
        ("battery_control_mode_standby", "Unknown", "mode|Unknown|option"),
        ("battery_control_mode_charge_grid_first", "Not A Real Mode", "mode|option"),
        ("battery_control_fallback_mode", "Unknown", "fallback|Unknown|mode"),
        ("battery_control_command_mode", "Standby", "command.*fallback|distinct|identical"),
        (
            "battery_control_discharge_command_mode",
            "Command Charging (Grid First)",
            "discharging mode",
        ),
        ("battery_control_export_command_mode", "Maximum Self Consumption", "discharging mode"),
        (
            "battery_control_export_pv_command_mode",
            "Maximum Self Consumption",
            "discharging mode",
        ),
        ("battery_control_discharge_command_mode", "Unknown", "mode|Unknown|option"),
        ("battery_control_max_charge_kw", 0.0, "charge.*kw|positive|non.?positive"),
        ("battery_control_max_discharge_kw", -1.0, "discharge.*kw|positive|non.?positive"),
        ("battery_control_physical_verify_timeout_seconds", 14.9, "15|physical|verify"),
        ("battery_control_cadence_seconds", 0, "cadence|positive|non.?positive"),
        ("battery_control_max_plan_age_seconds", -1, "plan.?age|positive|non.?positive"),
        ("battery_control_max_grid_export_kw", 100.0, "export.*limit|capability|site|inverter"),
        (
            "battery_control_min_soc_pct",
            99.0,
            "SoC|soc|ordered|reserve|maximum|minimum",
        ),
    ],
)
def test_battery_control_startup_rejects_invalid_config(
    field: str, value: object, match: str
) -> None:
    kwargs = _armed_control_kwargs(**{field: value})
    if field == "battery_control_command_mode":
        kwargs["battery_control_fallback_mode"] = "Standby"
        kwargs["battery_control_command_mode"] = "Standby"
    with pytest.raises(ValueError, match=match):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_battery_control_rejects_unknown_supported_direction() -> None:
    with pytest.raises(ValueError, match="supported.direction"):
        Settings(
            **_armed_control_kwargs(  # type: ignore[arg-type]
                battery_control_supported_directions=["FALLBACK", "IDLE", "TELEPORT"],
            )
        )


def test_authorize_discharge_requires_discharge_in_allowlist() -> None:
    with pytest.raises(ValueError, match="DISCHARGE|authorize_discharge|supported"):
        Settings(
            **_armed_control_kwargs(  # type: ignore[arg-type]
                battery_control_authorize_discharge=True,
                battery_control_supported_directions=["FALLBACK", "IDLE", "CHARGE"],
            )
        )
