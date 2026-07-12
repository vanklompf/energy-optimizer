from __future__ import annotations

import datetime as dt

from energy_optimizer.forecast.load import LoadForecaster, LoadSample
from energy_optimizer.forecast.price import PriceSample, pad_prices
from energy_optimizer.safety import SafetyInputs, Status, evaluate


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


def test_safety_low_confidence_on_padding() -> None:
    report = evaluate(
        SafetyInputs(
            telemetry_stale=False,
            telemetry_stale_reasons=[],
            have_current_price=True,
            have_pv_forecast=True,
            have_load_forecast=True,
            known_price_hours=10,
            horizon_hours=48,
        )
    )
    assert report.status == Status.LOW_CONFIDENCE


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
