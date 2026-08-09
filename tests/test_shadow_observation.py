from __future__ import annotations

import datetime as dt

from energy_optimizer.shadow_observation import ShadowAction, TelemetrySample, observe_shadow_action


def test_observe_shadow_charge_reports_matching_actual_direction() -> None:
    start = dt.datetime(2026, 8, 10, 10, 0, tzinfo=dt.UTC)

    observation = observe_shadow_action(
        ShadowAction(
            command_id="shadow-charge",
            interval_start=start,
            dt_hours=0.25,
            direction="CHARGE",
            requested_power_kw=0.5,
        ),
        [
            TelemetrySample(
                start + dt.timedelta(minutes=1), batt_charge_kw=0.45, batt_discharge_kw=0.0
            ),
            TelemetrySample(
                start + dt.timedelta(minutes=6), batt_charge_kw=0.52, batt_discharge_kw=0.0
            ),
        ],
    )

    assert observation.status == "match"
    assert observation.actual_direction == "CHARGE"
    assert observation.average_battery_kw == 0.485


def test_observe_shadow_rejects_insufficient_telemetry() -> None:
    start = dt.datetime(2026, 8, 10, 10, 0, tzinfo=dt.UTC)

    observation = observe_shadow_action(
        ShadowAction(
            command_id="shadow-idle",
            interval_start=start,
            dt_hours=0.25,
            direction="IDLE",
            requested_power_kw=0.0,
        ),
        [TelemetrySample(start + dt.timedelta(minutes=1), batt_discharge_kw=0.2)],
    )

    assert observation.status == "insufficient_telemetry"
    assert observation.actual_direction is None


def test_observe_shadow_does_not_zero_fill_missing_battery_channel() -> None:
    start = dt.datetime(2026, 8, 10, 10, 0, tzinfo=dt.UTC)

    observation = observe_shadow_action(
        ShadowAction("shadow-missing", start, 0.25, "CHARGE", 0.5),
        [
            TelemetrySample(start + dt.timedelta(minutes=1), batt_charge_kw=0.5),
            TelemetrySample(start + dt.timedelta(minutes=6), batt_charge_kw=0.5),
        ],
    )

    assert observation.status == "insufficient_telemetry"
    assert observation.sample_count == 0
