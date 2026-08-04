from __future__ import annotations

import datetime as dt

from energy_optimizer.config import Settings
from energy_optimizer.ev import build_ev_requirements, same_day_forecast_surplus_kwh


def test_ev_requirements_split_minimum_departure_and_full_target() -> None:
    settings = Settings(
        db=":memory:",
        tz="Europe/Warsaw",
        step_minutes=15,
        ev_capacity_kwh=10.9,
        ev_charge_power_kw=1.8,
        ev_charge_efficiency=0.90,
        ev_minimum_target_soc_pct=75,
        ev_departure_hour=7,
    )
    now = dt.datetime(2026, 8, 2, 18, 0, tzinfo=dt.UTC)  # 20:00 Warsaw
    starts = [now + dt.timedelta(minutes=15 * i) for i in range(96)]

    req = build_ev_requirements(50.0, now, starts, settings)

    assert req.departure_at == dt.datetime(2026, 8, 3, 7, 0, tzinfo=dt.UTC) - dt.timedelta(hours=2)
    # Fixed 15-minute, 1.8 kW slots: 0.45 kWh AC each.
    assert req.minimum_slots == 7  # 25% * 10.9 / 0.9 = 3.028 kWh => 7 slots
    assert req.target_slots == 14  # 50% * 10.9 / 0.9 = 6.056 kWh => 14 slots


def test_ev_requirements_are_zero_at_target() -> None:
    settings = Settings(db=":memory:")
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    starts = [now]

    req = build_ev_requirements(100.0, now, starts, settings)

    assert req.minimum_slots == 0
    assert req.target_slots == 0


def test_ev_requirements_report_departure_shortfall_and_use_every_available_slot() -> None:
    settings = Settings(
        db=":memory:",
        tz="Europe/Warsaw",
        step_minutes=15,
        ev_capacity_kwh=10.9,
        ev_charge_power_kw=1.8,
        ev_charge_efficiency=0.90,
        ev_minimum_target_soc_pct=50,
        ev_departure_hour=9,
    )
    now = dt.datetime(2026, 8, 3, 6, 30, tzinfo=dt.UTC)  # 08:30 Warsaw
    starts = [now, now + dt.timedelta(minutes=15)]

    req = build_ev_requirements(20.0, now, starts, settings)

    assert req.minimum_slots == 2
    assert req.minimum_shortfall_slots == 7
    assert req.target_shortfall_slots == 20


def test_ev_requirements_report_full_shortfall_when_no_slots_are_actionable() -> None:
    settings = Settings(
        db=":memory:",
        ev_minimum_target_soc_pct=50,
    )
    now = dt.datetime(2026, 8, 3, 5, 0, tzinfo=dt.UTC)

    req = build_ev_requirements(20.0, now, [], settings)

    assert req.minimum_slots == 0
    assert req.minimum_shortfall_slots > 0
    assert req.target_shortfall_slots > 0


def test_forecast_surplus_caps_only_opportunistic_slots() -> None:
    settings = Settings(
        db=":memory:",
        step_minutes=15,
        ev_capacity_kwh=10.9,
        ev_charge_power_kw=1.8,
        ev_charge_efficiency=0.9,
        ev_minimum_target_soc_pct=50,
    )
    now = dt.datetime(2026, 8, 3, 5, 0, tzinfo=dt.UTC)
    starts = [now + dt.timedelta(minutes=15 * i) for i in range(96)]

    req = build_ev_requirements(
        70.0,
        now,
        starts,
        settings,
        forecast_surplus_kwh=0.91,
    )

    assert req.minimum_slots == 0
    assert req.target_slots == 2


def test_expensive_window_cap_can_disable_opportunistic_slots() -> None:
    settings = Settings(db=":memory:", step_minutes=15)
    now = dt.datetime(2026, 8, 3, 5, 0, tzinfo=dt.UTC)
    starts = [now + dt.timedelta(minutes=15 * i) for i in range(24)]

    req = build_ev_requirements(
        70.0,
        now,
        starts,
        settings,
        forecast_surplus_kwh=10.0,
        max_opportunistic_slots=0,
    )

    assert req.minimum_slots == 0
    assert req.target_slots == 0


def test_guaranteed_minimum_is_not_reduced_by_missing_forecast_surplus() -> None:
    settings = Settings(
        db=":memory:",
        step_minutes=15,
        ev_capacity_kwh=10.9,
        ev_charge_power_kw=1.8,
        ev_charge_efficiency=0.9,
        ev_minimum_target_soc_pct=50,
    )
    now = dt.datetime(2026, 8, 3, 4, 45, tzinfo=dt.UTC)
    starts = [now + dt.timedelta(minutes=15 * i) for i in range(96)]

    req = build_ev_requirements(
        20.0,
        now,
        starts,
        settings,
        forecast_surplus_kwh=0.0,
    )

    assert req.minimum_slots == 9
    assert req.target_slots == 9


def test_same_day_forecast_surplus_is_conservative_and_excludes_tomorrow() -> None:
    now = dt.datetime(2026, 8, 3, 10, 0, tzinfo=dt.UTC)  # 12:00 Warsaw
    later = now + dt.timedelta(hours=1)
    tomorrow = now + dt.timedelta(hours=13)
    pv = {later: 3.0, tomorrow: 9.0}
    load = {later: 1.0, tomorrow: 0.0}

    usable = same_day_forecast_surplus_kwh(
        now,
        pv,
        load,
        tz="Europe/Warsaw",
        factor=0.8,
    )

    assert usable == 1.6
