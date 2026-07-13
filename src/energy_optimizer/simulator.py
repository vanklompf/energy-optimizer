"""Replay engine: apply a policy (or optimiser plan) to a historical series.

Used for backtests and counterfactuals. Given a time series of PV/load/prices and a
starting SoC, it walks each interval, asks the policy for flows, enforces SoC bounds,
and accumulates costs via :mod:`accounting`. The ``actual_sigen`` pseudo-policy instead
reconstructs flows from measured telemetry so "what actually happened" can be valued on
the same basis as counterfactuals.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .accounting import CostBreakdown, StepFlows, value_flows
from .optimiser import OptimiseResult
from .policies import BASELINE_POLICIES, FlowDecision, IntervalState, Policy


@dataclass(slots=True)
class SeriesInterval:
    interval_start: str
    dt_hours: float
    pv_energy_kwh: float
    load_energy_kwh: float
    buy_price: float
    sell_price: float
    # Optional measured flows (for the actual_sigen policy), all >= 0 (SoC/terminal side).
    measured_grid_import_kwh: float | None = None
    measured_grid_export_kwh: float | None = None
    measured_charge_kwh: float | None = None
    measured_discharge_kwh: float | None = None


@dataclass(slots=True)
class SimStep:
    interval_start: str
    dt_hours: float
    decision: FlowDecision
    grid_import_kwh: float
    grid_export_kwh: float
    battery_throughput_kwh: float
    soc_kwh_end: float


@dataclass(slots=True)
class SimResult:
    policy: str
    steps: list[SimStep] = field(default_factory=list)
    cost: CostBreakdown | None = None


@dataclass(slots=True)
class BatteryParams:
    capacity_kwh: float
    soc_min_kwh: float
    soc_max_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    eta_charge: float
    eta_discharge: float
    degradation_cost_pln_per_kwh: float = 0.05
    import_price_adjustment_pln_kwh: float = 0.0


def simulate_policy(
    series: list[SeriesInterval],
    policy: Policy,
    soc_start_kwh: float,
    battery: BatteryParams,
) -> SimResult:
    soc = soc_start_kwh
    steps: list[SimStep] = []
    flow_rows: list[StepFlows] = []

    for itv in series:
        state = IntervalState(
            dt_hours=itv.dt_hours,
            pv_energy_kwh=itv.pv_energy_kwh,
            load_energy_kwh=itv.load_energy_kwh,
            buy_price=itv.buy_price,
            sell_price=itv.sell_price,
            soc_kwh=soc,
            soc_min_kwh=battery.soc_min_kwh,
            soc_max_kwh=battery.soc_max_kwh,
            max_charge_kw=battery.max_charge_kw,
            max_discharge_kw=battery.max_discharge_kw,
            eta_charge=battery.eta_charge,
            eta_discharge=battery.eta_discharge,
        )
        d = policy.decide(state)
        soc_delta = d.pv_to_battery + d.grid_to_battery - d.battery_to_load - d.battery_to_grid
        soc = _clamp(soc + soc_delta, battery.soc_min_kwh, battery.soc_max_kwh)
        grid_import = d.grid_to_load + (
            d.grid_to_battery / battery.eta_charge if battery.eta_charge else 0.0
        )
        grid_export = d.pv_to_grid + d.battery_to_grid * battery.eta_discharge
        throughput = d.pv_to_battery + d.grid_to_battery + d.battery_to_load + d.battery_to_grid
        steps.append(
            SimStep(
                interval_start=itv.interval_start,
                dt_hours=itv.dt_hours,
                decision=d,
                grid_import_kwh=grid_import,
                grid_export_kwh=grid_export,
                battery_throughput_kwh=throughput,
                soc_kwh_end=soc,
            )
        )
        flow_rows.append(
            StepFlows(
                dt_hours=itv.dt_hours,
                buy_price=itv.buy_price,
                sell_price=itv.sell_price,
                grid_import_kwh=grid_import,
                grid_export_kwh=grid_export,
                battery_throughput_kwh=throughput,
            )
        )

    cost = value_flows(
        flow_rows,
        degradation_cost_pln_per_kwh=battery.degradation_cost_pln_per_kwh,
        import_price_adjustment_pln_kwh=battery.import_price_adjustment_pln_kwh,
    )
    return SimResult(policy=policy.name, steps=steps, cost=cost)


def value_actual(series: list[SeriesInterval], battery: BatteryParams) -> SimResult:
    """Value what actually happened, using measured telemetry flows."""
    flow_rows: list[StepFlows] = []
    steps: list[SimStep] = []
    for itv in series:
        imp = itv.measured_grid_import_kwh or 0.0
        exp = itv.measured_grid_export_kwh or 0.0
        throughput = (itv.measured_charge_kwh or 0.0) + (itv.measured_discharge_kwh or 0.0)
        flow_rows.append(
            StepFlows(
                dt_hours=itv.dt_hours,
                buy_price=itv.buy_price,
                sell_price=itv.sell_price,
                grid_import_kwh=imp,
                grid_export_kwh=exp,
                battery_throughput_kwh=throughput,
            )
        )
        steps.append(
            SimStep(
                interval_start=itv.interval_start,
                dt_hours=itv.dt_hours,
                decision=FlowDecision(),
                grid_import_kwh=imp,
                grid_export_kwh=exp,
                battery_throughput_kwh=throughput,
                soc_kwh_end=0.0,
            )
        )
    cost = value_flows(
        flow_rows,
        degradation_cost_pln_per_kwh=battery.degradation_cost_pln_per_kwh,
        import_price_adjustment_pln_kwh=battery.import_price_adjustment_pln_kwh,
    )
    return SimResult(policy="actual_sigen", steps=steps, cost=cost)


def value_optimiser_plan(
    result: OptimiseResult,
    series: list[SeriesInterval],
    battery: BatteryParams,
) -> CostBreakdown:
    """Value an optimiser plan against the same price series for comparison."""
    price_by_start = {itv.interval_start: itv for itv in series}
    flow_rows: list[StepFlows] = []
    for step in result.steps:
        itv = price_by_start.get(step.interval_start)
        buy = itv.buy_price if itv else 0.0
        sell = itv.sell_price if itv else 0.0
        throughput = (
            step.pv_to_battery_kwh
            + step.grid_to_battery_kwh
            + step.battery_to_load_kwh
            + step.battery_to_grid_kwh
        )
        flow_rows.append(
            StepFlows(
                dt_hours=step.dt_hours,
                buy_price=buy,
                sell_price=sell,
                grid_import_kwh=step.grid_import_kwh,
                grid_export_kwh=step.grid_export_kwh,
                battery_throughput_kwh=throughput,
            )
        )
    return value_flows(
        flow_rows,
        degradation_cost_pln_per_kwh=battery.degradation_cost_pln_per_kwh,
        import_price_adjustment_pln_kwh=battery.import_price_adjustment_pln_kwh,
    )


def get_policy(name: str) -> Policy:
    try:
        return BASELINE_POLICIES[name]
    except KeyError as exc:
        raise ValueError(f"unknown policy: {name!r}") from exc


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
