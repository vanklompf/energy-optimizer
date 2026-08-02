from __future__ import annotations

import math

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
    assert abs(s.soc_max_kwh - 18.08 * 0.98) < 1e-9


def test_step_hours() -> None:
    s = Settings(db=":memory:", step_minutes=15)
    assert s.step_hours == 0.25


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
    assert s.ev_target_soc_pct == 50.0
    assert s.ev_departure_hour == 9
    assert s.ev_min_on_minutes == 15
    assert s.ev_min_off_minutes == 5
    assert s.ev_power_start_grace_minutes == 5
    assert s.ev_min_charging_power_kw == 0.1
