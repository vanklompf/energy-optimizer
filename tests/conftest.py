from __future__ import annotations

import pytest

from energy_optimizer.config import PvPlane, Settings
from energy_optimizer.store import Store


@pytest.fixture
def settings() -> Settings:
    return Settings(
        db=":memory:",
        mqtt_enabled=False,
        ha_token="",
        pstryk_api_key="",
        battery_capacity_kwh=10.0,
        battery_max_charge_kw=5.0,
        battery_max_discharge_kw=5.0,
        battery_soc_min_pct=20.0,
        battery_soc_max_pct=98.0,
        battery_round_trip_efficiency=0.90,
        degradation_cost_pln_per_kwh=0.05,
        site_import_limit_kw=14.0,
        site_export_limit_kw=14.0,
        inverter_limit_kw=12.0,
        pv_planes=[PvPlane(peak_kwp=7.0, tilt=35, azimuth=0)],
    )


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.create_all()
    return s
