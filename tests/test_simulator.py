from __future__ import annotations

import math

from energy_optimizer.optimiser import PlanStepResult
from energy_optimizer.simulator import (
    BatteryParams,
    ControlSimParams,
    SeriesInterval,
    get_policy,
    simulate_control_aware,
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
        hard_min_kwh=0.0,
    )


def _series() -> list[SeriesInterval]:
    data = [
        (3.0, 1.0),
        (3.0, 1.0),
        (0.0, 2.0),
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


def _plan_charge_then_discharge() -> list[PlanStepResult]:
    steps = []
    for i, (chg, dis) in enumerate([(2.0, 0.0), (2.0, 0.0), (0.0, 2.0), (0.0, 2.0)]):
        steps.append(
            PlanStepResult(
                interval_start=f"2026-07-12T{i:02d}:00:00+00:00",
                dt_hours=1.0,
                pv_to_load_kwh=0.0,
                pv_to_ev_kwh=0.0,
                pv_to_battery_kwh=chg,
                pv_to_grid_kwh=0.0,
                grid_to_load_kwh=0.0,
                grid_to_ev_kwh=0.0,
                grid_to_battery_kwh=0.0,
                battery_to_load_kwh=dis,
                battery_to_grid_kwh=0.0,
                battery_to_ev_kwh=0.0,
                curtail_kwh=0.0,
                grid_import_kwh=0.0,
                grid_export_kwh=0.0,
                soc_kwh_end=5.0,
                soc_pct_end=50.0,
            )
        )
    return steps


def test_pv_only_never_touches_battery() -> None:
    result = simulate_policy(_series(), get_policy("pv_only"), 5.0, battery())
    assert all(s.battery_throughput_kwh == 0.0 for s in result.steps)
    assert result.cost is not None
    assert result.cost.export_kwh > 0
    assert result.cost.import_kwh > 0


def test_self_consumption_beats_pv_only() -> None:
    b = battery()
    pv_only = simulate_policy(_series(), get_policy("pv_only"), 5.0, b)
    self_cons = simulate_policy(_series(), get_policy("self_consumption"), 5.0, b)
    assert pv_only.cost is not None and self_cons.cost is not None
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
    assert abs(result.cost.net_cost_pln - 1.55) < 1e-9


def test_control_aware_applies_deadband_and_reports_baseline_delta() -> None:
    series = _series()
    plan = _plan_charge_then_discharge()
    plan[0] = PlanStepResult(
        interval_start=plan[0].interval_start,
        dt_hours=1.0,
        pv_to_load_kwh=0.0,
        pv_to_ev_kwh=0.0,
        pv_to_battery_kwh=0.05,
        pv_to_grid_kwh=0.0,
        grid_to_load_kwh=0.0,
        grid_to_ev_kwh=0.0,
        grid_to_battery_kwh=0.0,
        battery_to_load_kwh=0.0,
        battery_to_grid_kwh=0.0,
        battery_to_ev_kwh=0.0,
        curtail_kwh=0.0,
        grid_import_kwh=0.0,
        grid_export_kwh=0.0,
        soc_kwh_end=5.0,
        soc_pct_end=50.0,
    )
    result = simulate_control_aware(
        series,
        plan,
        5.0,
        battery(),
        ControlSimParams(deadband_kw=0.12, max_power_step_kw=5.0, max_ramp_kw_per_s=5.0),
    )
    assert result.steps[0].actuated_charge_kwh == 0.0
    assert result.cost is not None
    assert result.baseline_cost is not None
    assert result.delta_vs_baseline_pln is not None
    assert result.hard_floor_violations == 0


def test_control_aware_fallback_and_unsettled_incomplete() -> None:
    series = _series()
    series[1] = SeriesInterval(
        interval_start=series[1].interval_start,
        dt_hours=1.0,
        pv_energy_kwh=3.0,
        load_energy_kwh=1.0,
        buy_price=1.0,
        sell_price=0.3,
        settled=False,
    )
    plan = _plan_charge_then_discharge()
    result = simulate_control_aware(
        series,
        plan,
        5.0,
        battery(),
        ControlSimParams(
            force_fallback_starts=frozenset({series[2].interval_start}),
            max_power_step_kw=5.0,
            max_ramp_kw_per_s=5.0,
        ),
    )
    assert result.incomplete is True
    assert result.incomplete_reason == "unsettled_interval"
    assert result.fallback_count >= 1
    assert all(s.interval_start != series[1].interval_start for s in result.steps)
    assert any(s.fallback for s in result.steps)


def test_control_aware_command_delay_reduces_throughput() -> None:
    series = _series()[:1]
    plan = _plan_charge_then_discharge()[:1]
    full = simulate_control_aware(
        series,
        plan,
        5.0,
        battery(),
        ControlSimParams(
            command_delay_hours=0.0,
            deadband_kw=0.0,
            max_power_step_kw=10.0,
            max_ramp_kw_per_s=10.0,
        ),
    )
    delayed = simulate_control_aware(
        series,
        plan,
        5.0,
        battery(),
        ControlSimParams(
            command_delay_hours=0.5,
            deadband_kw=0.0,
            max_power_step_kw=10.0,
            max_ramp_kw_per_s=10.0,
        ),
    )
    assert delayed.command_throughput_kwh < full.command_throughput_kwh
