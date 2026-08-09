"""Read-only comparison of shadow battery intents with observed plant telemetry."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

_NEUTRAL_BAND_KW = 0.05
_MIN_SAMPLES = 2


@dataclass(frozen=True, slots=True)
class ShadowAction:
    command_id: str
    interval_start: dt.datetime
    dt_hours: float
    direction: str
    requested_power_kw: float


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    ts: dt.datetime
    batt_charge_kw: float | None = None
    batt_discharge_kw: float | None = None


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    command_id: str
    status: str
    actual_direction: str | None
    average_battery_kw: float | None
    sample_count: int


def observe_shadow_action(
    action: ShadowAction, samples: list[TelemetrySample]
) -> ShadowObservation:
    """Compare one shadow-plan interval with later read-only battery telemetry.

    This is strictly observational: samples are never used to issue a command.  Two
    in-interval samples are the minimum evidence required to classify the actual
    direction; a missing channel is not silently treated as zero.
    """
    interval_end = action.interval_start + dt.timedelta(hours=action.dt_hours)
    valid = [
        sample
        for sample in samples
        if action.interval_start <= sample.ts < interval_end
        and sample.batt_charge_kw is not None
        and sample.batt_discharge_kw is not None
    ]
    if len(valid) < _MIN_SAMPLES:
        return ShadowObservation(
            command_id=action.command_id,
            status="insufficient_telemetry",
            actual_direction=None,
            average_battery_kw=None,
            sample_count=len(valid),
        )

    readings = [
        sample.batt_charge_kw - sample.batt_discharge_kw
        for sample in valid
        if sample.batt_charge_kw is not None and sample.batt_discharge_kw is not None
    ]
    average = sum(readings) / len(readings)
    if average > _NEUTRAL_BAND_KW:
        actual_direction = "CHARGE"
    elif average < -_NEUTRAL_BAND_KW:
        actual_direction = "DISCHARGE"
    else:
        actual_direction = "IDLE"
    return ShadowObservation(
        command_id=action.command_id,
        status="match" if actual_direction == action.direction else "mismatch",
        actual_direction=actual_direction,
        average_battery_kw=round(average, 6),
        sample_count=len(valid),
    )
