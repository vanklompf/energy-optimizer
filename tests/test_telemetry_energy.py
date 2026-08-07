from __future__ import annotations

import datetime as dt

import pytest

from energy_optimizer.store import Telemetry
from energy_optimizer.telemetry_energy import complete_hourly_energy


def _row(hour: dt.datetime, minute: int, *, pv: float | None = 2.0) -> Telemetry:
    return Telemetry(
        ts=hour + dt.timedelta(minutes=minute),
        pv_kw=pv,
        batt_charge_kw=0.4,
        batt_discharge_kw=0.5,
        stale=False,
    )


def test_complete_hourly_energy_integrates_covered_power() -> None:
    hour = dt.datetime(2026, 8, 7, 7, tzinfo=dt.UTC)
    rows = [_row(hour, minute) for minute in (0, 15, 30, 45)]

    energy = complete_hourly_energy(rows)

    assert energy[hour]["pv"] == pytest.approx(2.0)
    assert energy[hour]["charge"] == pytest.approx(0.4)
    assert energy[hour]["discharge"] == pytest.approx(0.5)


def test_complete_hourly_energy_rejects_partial_or_missing_channel() -> None:
    hour = dt.datetime(2026, 8, 7, 7, tzinfo=dt.UTC)
    partial = [_row(hour, minute) for minute in (30, 45)]
    missing_pv = [_row(hour, minute, pv=None) for minute in (0, 15, 30, 45)]
    invalid_pv = [_row(hour, minute, pv=float("nan")) for minute in (0, 15, 30, 45)]

    assert complete_hourly_energy(partial) == {}
    assert complete_hourly_energy(missing_pv) == {}
    assert complete_hourly_energy(invalid_pv) == {}
