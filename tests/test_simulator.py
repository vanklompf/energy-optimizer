from __future__ import annotations

import math

from energy_optimizer.simulator import (
    BatteryParams,
    SeriesInterval,
    get_policy,
    simulate_policy,
    value_actual,
)


def battery() -> BatteryParams:
    return BatteryParams(
        capacity_kwh=10.0,
        soc_min_kwh=2.0,
        soc_max_kwh=9.8,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        eta_charge=math.sqrt(0.9),
        eta_discharge=math.sqrt(0.9),
        degradation_cost_pln_per_kwh=0.05,
    )


def _series() -> list[SeriesInterval]:
    # Morning PV surplus, evening deficit.
    data = [
        (3.0, 1.0),  # surplus 2
        (3.0, 1.0),
        (0.0, 2.0),  # deficit 2
        (0.0, 2.0),
    ]
    out = []
    for i, (pv, load) in enumerate(data):
        out.append(
            SeriesInterval(
                interval_start=f"2026-07-12T{i:02d}:00:00+00:00",
                dt_hours=1.0,
                pv_energy_kwh=pv,
                load_energy_kwh=load,
                buy_price=1.0,
                sell_price=0.3,
            )
        )
    return out


def test_pv_only_never_touches_battery() -> None:
    result = simulate_policy(_series(), get_policy("pv_only"), 5.0, battery())
    assert all(s.battery_throughput_kwh == 0.0 for s in result.steps)
    # PV surplus is exported; deficit imported.
    assert result.cost is not None
    assert result.cost.export_kwh > 0
    assert result.cost.import_kwh > 0


def test_self_consumption_beats_pv_only() -> None:
    b = battery()
    pv_only = simulate_policy(_series(), get_policy("pv_only"), 5.0, b)
    self_cons = simulate_policy(_series(), get_policy("self_consumption"), 5.0, b)
    assert pv_only.cost is not None and self_cons.cost is not None
    # Storing surplus to cover the evening deficit avoids buying at 1.0 and selling at 0.3.
    assert self_cons.cost.net_cost_pln < pv_only.cost.net_cost_pln


def test_self_consumption_respects_soc_max() -> None:
    b = battery()
    result = simulate_policy(_series(), get_policy("self_consumption"), 9.5, b)
    for s in result.steps:
        assert s.soc_kwh_end <= b.soc_max_kwh + 1e-9
        assert s.soc_kwh_end >= b.soc_min_kwh - 1e-9


def test_value_actual_uses_measured_flows() -> None:
    series = [
        SeriesInterval(
            interval_start="2026-07-12T00:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=1.0,
            sell_price=0.5,
            measured_grid_import_kwh=2.0,
            measured_grid_export_kwh=1.0,
            measured_charge_kwh=0.5,
            measured_discharge_kwh=0.5,
        )
    ]
    result = value_actual(series, battery())
    assert result.cost is not None
    # 2*1.0 import - 1*0.5 export + (0.5+0.5)*0.05 deg = 2 - 0.5 + 0.05 = 1.55
    assert abs(result.cost.net_cost_pln - 1.55) < 1e-9
