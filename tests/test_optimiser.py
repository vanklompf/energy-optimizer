from __future__ import annotations

import math

from energy_optimizer.optimiser import IntervalInput, OptimiserParams, optimise


def make_params(**overrides) -> OptimiserParams:
    base = dict(
        battery_capacity_kwh=10.0,
        soc_min_kwh=2.0,
        soc_max_kwh=9.8,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        eta_charge=math.sqrt(0.9),
        eta_discharge=math.sqrt(0.9),
        site_import_limit_kw=14.0,
        site_export_limit_kw=14.0,
        inverter_limit_kw=None,
        degradation_cost_pln_per_kwh=0.05,
    )
    base.update(overrides)
    return OptimiserParams(**base)


def test_empty_intervals_returns_error() -> None:
    result = optimise([], 2.0, make_params())
    assert result.status == "error"


def test_arbitrage_charges_cheap_discharges_expensive() -> None:
    intervals = []
    for h in range(6):
        buy = 0.2 if h < 3 else 2.0
        intervals.append(
            IntervalInput(
                interval_start=f"2026-07-12T{h:02d}:00:00+00:00",
                dt_hours=1.0,
                pv_energy_kwh=0.0,
                load_energy_kwh=1.0,
                buy_price=buy,
                sell_price=buy * 0.5,
            )
        )
    result = optimise(intervals, 2.0, make_params())
    assert result.status == "optimal"
    # It should grid-charge during the cheap window and discharge during the expensive one.
    early_charge = sum(s.grid_to_battery_kwh for s in result.steps[:3])
    late_discharge = sum(s.battery_to_load_kwh for s in result.steps[3:])
    assert early_charge > 0.5
    assert late_discharge > 0.5


def test_no_simultaneous_charge_and_discharge() -> None:
    intervals = [
        IntervalInput(
            interval_start=f"2026-07-12T{h:02d}:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=2.0,
            load_energy_kwh=1.0,
            buy_price=1.0,
            sell_price=-0.1,  # negative sell price: must not burn energy
        )
        for h in range(4)
    ]
    result = optimise(intervals, 5.0, make_params())
    assert result.status == "optimal"
    for s in result.steps:
        charge = s.pv_to_battery_kwh + s.grid_to_battery_kwh
        discharge = s.battery_to_load_kwh + s.battery_to_grid_kwh
        assert not (charge > 1e-6 and discharge > 1e-6), "simultaneous charge/discharge"


def test_battery_export_flag_disables_export() -> None:
    intervals = [
        IntervalInput(
            interval_start=f"2026-07-12T{h:02d}:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=0.1,
            sell_price=5.0,  # very lucrative export
        )
        for h in range(3)
    ]
    result = optimise(intervals, 9.0, make_params(allow_battery_export=False))
    assert result.status == "optimal"
    assert all(s.battery_to_grid_kwh == 0.0 for s in result.steps)


def test_soc_respects_bounds() -> None:
    intervals = [
        IntervalInput(
            interval_start=f"2026-07-12T{h:02d}:00:00+00:00",
            dt_hours=0.25,
            pv_energy_kwh=0.0,
            load_energy_kwh=2.0,
            buy_price=1.0,
            sell_price=0.5,
        )
        for h in range(8)
    ]
    params = make_params()
    result = optimise(intervals, 9.0, params)
    assert result.status == "optimal"
    for s in result.steps:
        assert s.soc_kwh_end >= params.soc_min_kwh - 1e-6
        assert s.soc_kwh_end <= params.soc_max_kwh + 1e-6
