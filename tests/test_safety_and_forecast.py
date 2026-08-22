from __future__ import annotations

import datetime as dt
import math

import httpx
import pytest

from energy_optimizer.config import PvPlane
from energy_optimizer.forecast.load import LoadForecaster, LoadSample
from energy_optimizer.forecast.price import PriceSample, pad_prices
from energy_optimizer.forecast.pv import PvForecaster
from energy_optimizer.safety import (
    SafetyInputs,
    Status,
    evaluate,
    select_current_interval,
)


def test_safety_blocks_on_missing_price() -> None:
    report = evaluate(
        SafetyInputs(
            telemetry_stale=False,
            telemetry_stale_reasons=[],
            have_current_price=False,
            have_pv_forecast=True,
            have_load_forecast=True,
            known_price_hours=48,
            horizon_hours=48,
        )
    )
    assert report.status == Status.BLOCKED
    assert report.control_enabled is False
    assert report.control_authorized is False


def test_safety_low_confidence_when_price_coverage_is_abnormally_short() -> None:
    # Below the day-ahead floor means publication or the fetch job is failing.
    report = evaluate(
        SafetyInputs(
            telemetry_stale=False,
            telemetry_stale_reasons=[],
            have_current_price=True,
            have_pv_forecast=True,
            have_load_forecast=True,
            known_price_hours=3,
            horizon_hours=3,
            min_price_hours=8,
        )
    )
    assert report.status == Status.LOW_CONFIDENCE
    assert any("Pstryk prices" in w for w in report.warnings)
    assert report.control_authorized is False


def test_short_morning_horizon_is_not_a_plan_quality_problem() -> None:
    # Pstryk publishes day-ahead, so ~11h of forward coverage before the afternoon
    # publication is the normal cycle. Treating it as low-confidence blocked every
    # economic command permanently.
    report = evaluate(
        SafetyInputs(
            telemetry_stale=False,
            telemetry_stale_reasons=[],
            have_current_price=True,
            have_pv_forecast=True,
            have_load_forecast=True,
            known_price_hours=11,
            horizon_hours=11,
            min_price_hours=8,
        )
    )
    assert report.status == Status.OK
    assert report.warnings == []


def test_safety_ok() -> None:
    report = evaluate(
        SafetyInputs(
            telemetry_stale=False,
            telemetry_stale_reasons=[],
            have_current_price=True,
            have_pv_forecast=True,
            have_load_forecast=True,
            known_price_hours=48,
            horizon_hours=48,
        )
    )
    assert report.status == Status.OK
    assert report.control_authorized is False  # gates still off by default


def test_low_confidence_plan_is_publishable_but_not_live_arbitrage() -> None:
    now = dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC)
    report = evaluate(
        _authorized_control_inputs(
            now,
            have_pv_forecast=False,
            economic_action=True,
        )
    )
    assert report.status == Status.LOW_CONFIDENCE
    assert "plan_not_ok" in report.control_blockers
    assert report.control_authorized is False


def _authorized_control_inputs(now: dt.datetime, **overrides: object) -> SafetyInputs:
    start = now.replace(minute=0, second=0, microsecond=0)
    base: dict[str, object] = dict(
        telemetry_stale=False,
        telemetry_stale_reasons=[],
        have_current_price=True,
        have_pv_forecast=True,
        have_load_forecast=True,
        known_price_hours=48,
        horizon_hours=48,
        plan_status_ok=True,
        plan_age_seconds=30.0,
        max_plan_age_seconds=900.0,
        current_interval_start=start,
        current_interval_end=start + dt.timedelta(minutes=15),
        now=now,
        current_buy_price=0.5,
        current_sell_price=0.2,
        current_price_is_real=True,
        current_price_age_seconds=60.0,
        telemetry_ages_seconds={"battery_power": 10.0, "grid_import": 10.0},
        max_telemetry_age_seconds=120.0,
        inverter_entities_available=True,
        ev_goal_active=False,
        ev_telemetry_fresh=True,
        lease_held=True,
        watchdog_healthy=True,
        recent_command_failures=0,
        soc_pct=55.0,
        soc_update_age_seconds=20.0,
        soc_at_boundary=False,
        corroborating_power_fresh=True,
        battery_control_enabled=True,
        mode_is_control=True,
        economic_action=True,
    )
    base.update(overrides)
    return SafetyInputs(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value,blocker",
    [
        ("mode_is_control", False, "mode_not_control"),
        ("battery_control_enabled", False, "battery_control_disabled"),
        ("lease_held", False, "lease_not_held"),
        ("watchdog_healthy", False, "watchdog_unhealthy"),
        ("recent_command_failures", 5, "recent_command_failures"),
        ("inverter_entities_available", False, "inverter_entities_unavailable"),
        ("plan_age_seconds", 901.0, "plan_stale"),
        ("current_price_is_real", False, "current_price_not_real"),
        ("current_buy_price", math.nan, "current_price_unavailable"),
        ("current_price_age_seconds", 4000.0, "current_price_stale"),
        ("soc_pct", None, "soc_not_fresh"),
        ("soc_update_age_seconds", 500.0, "soc_not_fresh"),
        ("ev_goal_active", True, "ev_telemetry_stale"),
    ],
)
def test_control_authorization_blockers(field: str, value: object, blocker: str) -> None:
    now = dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC)
    overrides: dict[str, object] = {field: value}
    if field == "ev_goal_active":
        overrides["ev_telemetry_fresh"] = False
    report = evaluate(_authorized_control_inputs(now, **overrides))
    assert blocker in report.control_blockers
    assert report.control_authorized is False


def test_control_authorized_when_all_gates_pass() -> None:
    now = dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC)
    report = evaluate(_authorized_control_inputs(now))
    assert report.status == Status.OK
    assert report.control_authorized is True
    assert report.control_blockers == []
    assert report.control_enabled is True


def test_idle_fallback_skips_forecast_confidence() -> None:
    now = dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC)
    report = evaluate(
        _authorized_control_inputs(
            now,
            have_pv_forecast=False,
            economic_action=False,
            current_price_is_real=False,
            plan_age_seconds=None,
        )
    )
    assert report.status == Status.LOW_CONFIDENCE
    assert report.control_authorized is True
    assert "plan_not_ok" not in report.control_blockers


def test_boundary_pinned_soc_fresh_with_corroborating_telemetry() -> None:
    now = dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC)
    report = evaluate(
        _authorized_control_inputs(
            now,
            soc_pct=100.0,
            soc_at_boundary=True,
            soc_update_age_seconds=30.0,
            corroborating_power_fresh=True,
        )
    )
    assert report.control_authorized is True


def test_boundary_pinned_soc_stale_without_corroborating_telemetry() -> None:
    now = dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC)
    report = evaluate(
        _authorized_control_inputs(
            now,
            soc_pct=100.0,
            soc_at_boundary=True,
            soc_update_age_seconds=30.0,
            corroborating_power_fresh=False,
        )
    )
    assert "soc_not_fresh" in report.control_blockers


def test_unchanged_stale_soc_is_not_fresh() -> None:
    now = dt.datetime(2026, 8, 8, 12, 5, tzinfo=dt.UTC)
    report = evaluate(
        _authorized_control_inputs(
            now,
            soc_pct=100.0,
            soc_at_boundary=True,
            soc_update_age_seconds=500.0,
            corroborating_power_fresh=True,
        )
    )
    assert "soc_not_fresh" in report.control_blockers


def test_select_current_interval_dst_aware() -> None:
    # Europe/Warsaw spring forward 2026-03-29: 02:00 -> 03:00 local.
    starts = [
        dt.datetime(2026, 3, 29, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 3, 29, 0, 15, tzinfo=dt.UTC),
        dt.datetime(2026, 3, 29, 0, 30, tzinfo=dt.UTC),
    ]
    now = dt.datetime(2026, 3, 29, 0, 20, tzinfo=dt.UTC)
    selected = select_current_interval(starts, now=now, step_minutes=15, tz="Europe/Warsaw")
    assert selected is not None
    assert selected[0] == starts[1]

    missing = select_current_interval(
        starts,
        now=dt.datetime(2026, 3, 28, 23, 0, tzinfo=dt.UTC),
        step_minutes=15,
    )
    assert missing is None


def test_price_padding_marks_forecast_low_confidence() -> None:
    tz = "Europe/Warsaw"
    base = dt.datetime(2026, 7, 12, 0, 0, tzinfo=dt.UTC)
    known = [PriceSample(base, buy=1.0, sell=0.5)]
    history = [
        PriceSample(base - dt.timedelta(days=d, hours=-1), buy=2.0, sell=0.8) for d in range(1, 8)
    ]
    targets = [base, base + dt.timedelta(hours=1)]
    result = pad_prices(known, history, targets, tz=tz)
    assert result[0].source == "api"
    assert result[0].confidence == "ok"
    assert result[1].source == "forecast"
    assert result[1].confidence == "low_confidence"


def test_load_forecast_confidence_scales_with_history() -> None:
    fc = LoadForecaster(tz="Europe/Warsaw")
    base = dt.datetime(2026, 7, 6, 10, 0, tzinfo=dt.UTC)  # a Monday
    samples = [LoadSample(base - dt.timedelta(days=d), load_kw=2.0) for d in range(0, 5)]
    targets = [(dt.datetime(2026, 7, 13, 10, 0, tzinfo=dt.UTC), 1.0)]
    out = fc.forecast(samples, targets)
    assert out[0].load_kwh == 2.0
    assert out[0].confidence == "ok"
    assert out[0].sample_count == 3
    assert out[0].required_sample_count == 3


def test_load_forecast_requires_three_distinct_local_dates() -> None:
    fc = LoadForecaster(tz="Europe/Warsaw")
    target = dt.datetime(2026, 7, 13, 10, 0, tzinfo=dt.UTC)
    samples = [
        LoadSample(target - dt.timedelta(days=7), load_kw=1.0),
        LoadSample(target - dt.timedelta(days=7, minutes=-15), load_kw=2.0),
        LoadSample(target - dt.timedelta(days=14), load_kw=3.0),
    ]

    point = fc.forecast(samples, [(target, 1.0)])[0]

    assert point.confidence == "low_confidence"
    assert point.sample_count == 2

    samples.append(LoadSample(target - dt.timedelta(days=21), load_kw=4.0))
    point = fc.forecast(samples, [(target, 1.0)])[0]
    assert point.confidence == "ok"
    assert point.sample_count == 3


async def test_successful_uncorrected_pv_provider_forecast_is_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"2026-08-22 12:00:00": 1500}},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with PvForecaster(
            50.0,
            22.0,
            [PvPlane(peak_kwp=7.0, tilt=35.0, azimuth=0.0)],
            client=client,
        ) as forecaster:
            points = await forecaster.forecast()

    assert len(points) == 1
    assert points[0].energy_kwh == pytest.approx(1.5)
    assert points[0].confidence == "ok"


@pytest.mark.parametrize(("ratio", "expected_kwh"), [(0.25, 0.75), (2.0, 2.25)])
async def test_pv_provider_clamps_correction_ratio(ratio: float, expected_kwh: float) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"2026-08-22 12:00:00": 1500}},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with PvForecaster(
            50.0,
            22.0,
            [PvPlane(peak_kwp=7.0, tilt=35.0, azimuth=0.0)],
            client=client,
        ) as forecaster:
            points = await forecaster.forecast(correction_ratio=ratio)

    assert points[0].energy_kwh == pytest.approx(expected_kwh)
