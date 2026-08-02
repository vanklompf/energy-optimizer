from __future__ import annotations

import datetime as dt

from energy_optimizer.config import Settings
from energy_optimizer.ev import build_ev_requirements


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
