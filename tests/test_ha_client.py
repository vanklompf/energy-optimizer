from __future__ import annotations

import datetime as dt

from energy_optimizer.ha_client import (
    ENTITY_BATTERY_POWER,
    ENTITY_CONSUMED_POWER,
    ENTITY_EMS_MODE,
    ENTITY_GRID_EXPORT_POWER,
    ENTITY_GRID_IMPORT_POWER,
    ENTITY_PV_POWER,
    ENTITY_SOC,
    HaState,
    _split_battery_power,
    build_snapshot,
)


def _state(entity: str, state: str, age_s: int, now: dt.datetime) -> HaState:
    return HaState(
        entity_id=entity,
        state=state,
        last_updated=now - dt.timedelta(seconds=age_s),
        attributes={},
    )


def test_split_battery_power_sign_convention() -> None:
    assert _split_battery_power(3.0) == (3.0, 0.0)  # >0 charging
    assert _split_battery_power(-2.5) == (0.0, 2.5)  # <0 discharging
    assert _split_battery_power(None) == (None, None)


def test_snapshot_fresh_is_not_stale() -> None:
    now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC)
    states = {
        ENTITY_SOC: _state(ENTITY_SOC, "55", 30, now),
        ENTITY_BATTERY_POWER: _state(ENTITY_BATTERY_POWER, "-1.2", 30, now),
        ENTITY_PV_POWER: _state(ENTITY_PV_POWER, "4.0", 30, now),
        ENTITY_CONSUMED_POWER: _state(ENTITY_CONSUMED_POWER, "1.5", 30, now),
        ENTITY_GRID_IMPORT_POWER: _state(ENTITY_GRID_IMPORT_POWER, "0.0", 30, now),
        ENTITY_GRID_EXPORT_POWER: _state(ENTITY_GRID_EXPORT_POWER, "1.3", 30, now),
        ENTITY_EMS_MODE: _state(ENTITY_EMS_MODE, "Self Consumption", 3600, now),
    }
    snap = build_snapshot(states, now)
    assert snap.stale is False
    assert snap.soc_pct == 55
    assert snap.batt_discharge_kw == 1.2
    assert snap.batt_charge_kw == 0.0
    assert snap.ems_mode == "Self Consumption"


def test_snapshot_stale_power_flags() -> None:
    now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC)
    states = {
        ENTITY_SOC: _state(ENTITY_SOC, "55", 30, now),
        ENTITY_BATTERY_POWER: _state(ENTITY_BATTERY_POWER, "1.0", 400, now),  # >5min
        ENTITY_PV_POWER: _state(ENTITY_PV_POWER, "4.0", 30, now),
        ENTITY_CONSUMED_POWER: _state(ENTITY_CONSUMED_POWER, "1.5", 30, now),
        ENTITY_GRID_IMPORT_POWER: _state(ENTITY_GRID_IMPORT_POWER, "0.0", 30, now),
        ENTITY_GRID_EXPORT_POWER: _state(ENTITY_GRID_EXPORT_POWER, "1.3", 30, now),
        ENTITY_EMS_MODE: _state(ENTITY_EMS_MODE, "Self Consumption", 3600, now),
    }
    snap = build_snapshot(states, now)
    assert snap.stale is True
    assert any("battery power" in r for r in snap.stale_reasons)


def test_snapshot_zero_power_sensor_is_not_stale() -> None:
    # PV pinned at 0 overnight (and an idle battery) stop emitting HA updates; a valid
    # numeric zero must not mark the snapshot stale and block the optimiser.
    now = dt.datetime(2026, 7, 12, 23, 0, tzinfo=dt.UTC)
    states = {
        ENTITY_SOC: _state(ENTITY_SOC, "44", 60, now),
        ENTITY_BATTERY_POWER: _state(ENTITY_BATTERY_POWER, "0.0", 3600, now),
        ENTITY_PV_POWER: _state(ENTITY_PV_POWER, "0.0", 14400, now),
        ENTITY_CONSUMED_POWER: _state(ENTITY_CONSUMED_POWER, "0.2", 30, now),
        ENTITY_GRID_IMPORT_POWER: _state(ENTITY_GRID_IMPORT_POWER, "0.0", 3600, now),
        ENTITY_GRID_EXPORT_POWER: _state(ENTITY_GRID_EXPORT_POWER, "0.0", 3600, now),
        ENTITY_EMS_MODE: _state(ENTITY_EMS_MODE, "Custom", 3600, now),
    }
    snap = build_snapshot(states, now)
    assert snap.stale is False
    assert snap.pv_kw == 0.0


def test_snapshot_missing_soc_is_stale() -> None:
    now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC)
    states = {
        ENTITY_SOC: _state(ENTITY_SOC, "unavailable", 30, now),
        ENTITY_BATTERY_POWER: _state(ENTITY_BATTERY_POWER, "1.0", 30, now),
        ENTITY_PV_POWER: _state(ENTITY_PV_POWER, "4.0", 30, now),
        ENTITY_CONSUMED_POWER: _state(ENTITY_CONSUMED_POWER, "1.5", 30, now),
        ENTITY_GRID_IMPORT_POWER: _state(ENTITY_GRID_IMPORT_POWER, "0.0", 30, now),
        ENTITY_GRID_EXPORT_POWER: _state(ENTITY_GRID_EXPORT_POWER, "1.3", 30, now),
        ENTITY_EMS_MODE: _state(ENTITY_EMS_MODE, "Self Consumption", 3600, now),
    }
    snap = build_snapshot(states, now)
    assert snap.stale is True
    assert snap.soc_pct is None
