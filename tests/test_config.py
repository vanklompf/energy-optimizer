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
