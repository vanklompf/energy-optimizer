"""Coverage-checked integration of live power telemetry into hourly energy."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence

from .store import Telemetry

MAX_SAMPLE_GAP = dt.timedelta(minutes=15)
_FIELDS = {
    "pv": "pv_kw",
    "charge": "batt_charge_kw",
    "discharge": "batt_discharge_kw",
}


def complete_hourly_energy(rows: Sequence[Telemetry]) -> dict[dt.datetime, dict[str, float]]:
    """Return only UTC hours with complete PV/charge/discharge coverage.

    Power is integrated as a left-continuous step function. An hour is rejected when
    any required channel is absent or when its boundary/sample gap exceeds 15 minutes.
    """
    buckets: dict[dt.datetime, list[Telemetry]] = {}
    for row in rows:
        if row.stale:
            continue
        ts = _aware(row.ts)
        hour = ts.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(row)

    result: dict[dt.datetime, dict[str, float]] = {}
    for hour, samples in buckets.items():
        energy: dict[str, float] = {}
        for name, field in _FIELDS.items():
            value = _integrate_hour(samples, field, hour)
            if value is None:
                break
            energy[name] = value
        if len(energy) == len(_FIELDS):
            result[hour] = energy
    return result


def _integrate_hour(rows: Sequence[Telemetry], field: str, hour: dt.datetime) -> float | None:
    end = hour + dt.timedelta(hours=1)
    points: list[tuple[dt.datetime, float]] = []
    for row in rows:
        raw = getattr(row, field)
        if raw is None:
            continue
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            return None
        timestamp = _aware(row.ts)
        if hour <= timestamp < end:
            points.append((timestamp, value))
    points.sort()
    if not points:
        return None
    if points[0][0] - hour > MAX_SAMPLE_GAP or end - points[-1][0] > MAX_SAMPLE_GAP:
        return None

    energy_kwh = 0.0
    previous_sample_at = points[0][0]
    cursor = hour
    value = points[0][1]
    for sample_at, next_value in points[1:]:
        if sample_at - previous_sample_at > MAX_SAMPLE_GAP:
            return None
        energy_kwh += value * (sample_at - cursor).total_seconds() / 3600.0
        cursor = sample_at
        previous_sample_at = sample_at
        value = next_value
    energy_kwh += value * (end - cursor).total_seconds() / 3600.0
    return energy_kwh


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
