"""Replay engine: apply a policy (or optimiser plan) to a historical series.

Used for backtests and counterfactuals. Given a time series of PV/load/prices and a
starting SoC, it walks each interval, asks the policy for flows, enforces SoC bounds,
and accumulates costs via :mod:`accounting`. Actual valuation uses only settled Pstryk
billing-meter intervals; missing intervals are omitted rather than replaced with inverter
telemetry.

Control-aware replay (:func:`simulate_control_aware`) additionally models command delay,
deadband, power ramp/step limits, and forced fallback so economics are not taken from
ideal optimiser flows alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .accounting import CostBreakdown, StepFlows, value_flows
from .optimiser import OptimiseResult, PlanStepResult
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
    # Optional settled-billing marker. Unsettled intervals are excluded from economic claims.
    settled: bool = True


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
    # Absolute usable floor (kWh). Harder than the operating reserve.
    hard_min_kwh: float = 0.0


@dataclass(slots=True)
class ControlSimParams:
    """Actuation constraints applied on top of ideal plan flows."""

    command_delay_hours: float = 0.0
    deadband_kw: float = 0.12
    max_power_step_kw: float = 0.5
    max_ramp_kw_per_s: float = 0.5
    force_fallback_starts: frozenset[str] = field(default_factory=frozenset)
    require_settled: bool = True


@dataclass(slots=True)
class ControlSimStep:
    interval_start: str
    dt_hours: float
    requested_charge_kwh: float
    requested_discharge_kwh: float
    actuated_charge_kwh: float
    actuated_discharge_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_throughput_kwh: float
    soc_kwh_end: float
    fallback: bool
    reserve_violation: bool
    hard_floor_violation: bool


@dataclass(slots=True)
class ControlSimResult:
    policy: str
    steps: list[ControlSimStep] = field(default_factory=list)
    cost: CostBreakdown | None = None
    baseline_cost: CostBreakdown | None = None
    delta_vs_baseline_pln: float | None = None
    fallback_count: int = 0
    reserve_violations: int = 0
    hard_floor_violations: int = 0
    ev_target_misses: int = 0
    command_throughput_kwh: float = 0.0
    incomplete: bool = False
    incomplete_reason: str | None = None


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


def simulate_control_aware(
    series: list[SeriesInterval],
    plan_steps: list[PlanStepResult],
    soc_start_kwh: float,
    battery: BatteryParams,
    control: ControlSimParams | None = None,
    *,
    baseline_policy: str = "self_consumption",
    ev_departure_soc_kwh: float | None = None,
    ev_departure_interval_start: str | None = None,
) -> ControlSimResult:
    """Replay a plan through actuation constraints; value only settled intervals.

    Ideal optimiser flows are clipped by deadband, delayed by ``command_delay_hours``,
    and ramp/step-limited. Forced fallback intervals contribute zero commanded battery
    power.
    """
    control = control or ControlSimParams()
    by_start = {p.interval_start: p for p in plan_steps}
    soc = soc_start_kwh
    previous_kw = 0.0
    steps: list[ControlSimStep] = []
    settled_flows: list[StepFlows] = []
    fallback_count = 0
    reserve_violations = 0
    hard_floor_violations = 0
    incomplete = False
    incomplete_reason: str | None = None
    command_throughput = 0.0

    for itv in series:
        if control.require_settled and not itv.settled:
            incomplete = True
            incomplete_reason = "unsettled_interval"
            continue
        plan = by_start.get(itv.interval_start)
        req_charge = 0.0
        req_discharge = 0.0
        if plan is not None:
            req_charge = plan.pv_to_battery_kwh + plan.grid_to_battery_kwh
            req_discharge = (
                plan.battery_to_load_kwh + plan.battery_to_grid_kwh + plan.battery_to_ev_kwh
            )

        fallback = itv.interval_start in control.force_fallback_starts or plan is None
        if fallback:
            fallback_count += 1
            act_charge = 0.0
            act_discharge = 0.0
            previous_kw = 0.0
        else:
            act_charge, act_discharge, previous_kw = _actuate_interval(
                requested_charge_kwh=req_charge,
                requested_discharge_kwh=req_discharge,
                dt_hours=itv.dt_hours,
                previous_kw=previous_kw,
                battery=battery,
                control=control,
                soc_kwh=soc,
            )

        pv = itv.pv_energy_kwh
        load = itv.load_energy_kwh
        pv_to_load = min(pv, load)
        remaining_load = load - pv_to_load
        batt_to_load = min(act_discharge, remaining_load)
        remaining_discharge = act_discharge - batt_to_load
        grid_to_load = max(0.0, remaining_load - batt_to_load)
        pv_left = pv - pv_to_load
        pv_to_batt = min(pv_left, act_charge)
        grid_to_batt = max(0.0, act_charge - pv_to_batt)
        batt_to_grid = remaining_discharge
        pv_to_grid = max(0.0, pv_left - pv_to_batt)

        grid_import = grid_to_load + (
            grid_to_batt / battery.eta_charge if battery.eta_charge else 0.0
        )
        grid_export = pv_to_grid + batt_to_grid * battery.eta_discharge
        throughput = act_charge + act_discharge
        command_throughput += throughput

        soc = _clamp(
            soc + act_charge - act_discharge,
            battery.hard_min_kwh,
            battery.soc_max_kwh,
        )
        reserve_violation = soc + 1e-9 < battery.soc_min_kwh
        hard_violation = soc + 1e-9 < battery.hard_min_kwh
        if reserve_violation:
            reserve_violations += 1
        if hard_violation:
            hard_floor_violations += 1

        steps.append(
            ControlSimStep(
                interval_start=itv.interval_start,
                dt_hours=itv.dt_hours,
                requested_charge_kwh=req_charge,
                requested_discharge_kwh=req_discharge,
                actuated_charge_kwh=act_charge,
                actuated_discharge_kwh=act_discharge,
                grid_import_kwh=grid_import,
                grid_export_kwh=grid_export,
                battery_throughput_kwh=throughput,
                soc_kwh_end=soc,
                fallback=fallback,
                reserve_violation=reserve_violation,
                hard_floor_violation=hard_violation,
            )
        )
        settled_flows.append(
            StepFlows(
                dt_hours=itv.dt_hours,
                buy_price=itv.buy_price,
                sell_price=itv.sell_price,
                grid_import_kwh=grid_import,
                grid_export_kwh=grid_export,
                battery_throughput_kwh=throughput,
            )
        )

    cost = (
        value_flows(
            settled_flows,
            degradation_cost_pln_per_kwh=battery.degradation_cost_pln_per_kwh,
            import_price_adjustment_pln_kwh=battery.import_price_adjustment_pln_kwh,
        )
        if settled_flows
        else None
    )

    baseline_series = [s for s in series if s.settled or not control.require_settled]
    baseline = simulate_policy(
        baseline_series,
        get_policy(baseline_policy),
        soc_start_kwh,
        battery,
    )
    delta = None
    if cost is not None and baseline.cost is not None:
        delta = baseline.cost.net_cost_pln - cost.net_cost_pln

    ev_misses = 0
    if ev_departure_soc_kwh is not None and ev_departure_interval_start is not None:
        for step in steps:
            if step.interval_start >= ev_departure_interval_start:
                if step.soc_kwh_end + 1e-9 < ev_departure_soc_kwh:
                    ev_misses = 1
                break

    if hard_floor_violations:
        incomplete = True
        incomplete_reason = incomplete_reason or "hard_floor_violation"

    return ControlSimResult(
        policy="control_aware",
        steps=steps,
        cost=cost,
        baseline_cost=baseline.cost,
        delta_vs_baseline_pln=delta,
        fallback_count=fallback_count,
        reserve_violations=reserve_violations,
        hard_floor_violations=hard_floor_violations,
        ev_target_misses=ev_misses,
        command_throughput_kwh=command_throughput,
        incomplete=incomplete,
        incomplete_reason=incomplete_reason,
    )


def _actuate_interval(
    *,
    requested_charge_kwh: float,
    requested_discharge_kwh: float,
    dt_hours: float,
    previous_kw: float,
    battery: BatteryParams,
    control: ControlSimParams,
    soc_kwh: float,
) -> tuple[float, float, float]:
    if dt_hours <= 0:
        return 0.0, 0.0, 0.0
    effective_hours = max(0.0, dt_hours - control.command_delay_hours)
    if effective_hours <= 0:
        return 0.0, 0.0, 0.0

    if requested_charge_kwh > 1e-9 and requested_discharge_kwh > 1e-9:
        return 0.0, 0.0, 0.0

    if requested_charge_kwh > requested_discharge_kwh:
        target_kw = requested_charge_kwh / dt_hours
        sign = 1.0
    elif requested_discharge_kwh > 0:
        target_kw = requested_discharge_kwh / dt_hours
        sign = -1.0
    else:
        return 0.0, 0.0, 0.0

    if target_kw < control.deadband_kw:
        return 0.0, 0.0, 0.0

    max_delta = control.max_power_step_kw + control.max_ramp_kw_per_s * effective_hours * 3600.0
    signed_prev = previous_kw
    signed_target = sign * target_kw
    delta = signed_target - signed_prev
    if abs(delta) > max_delta:
        signed_target = signed_prev + max_delta * (1.0 if delta > 0 else -1.0)

    if signed_target > 0:
        cap = battery.max_charge_kw
        headroom = max(0.0, battery.soc_max_kwh - soc_kwh)
        kw = min(signed_target, cap, headroom / effective_hours if effective_hours else 0.0)
        return kw * effective_hours, 0.0, kw
    if signed_target < 0:
        cap = battery.max_discharge_kw
        headroom = max(0.0, soc_kwh - battery.hard_min_kwh)
        kw = min(-signed_target, cap, headroom / effective_hours if effective_hours else 0.0)
        return 0.0, kw * effective_hours, -kw
    return 0.0, 0.0, 0.0


def value_actual(series: list[SeriesInterval], battery: BatteryParams) -> SimResult:
    """Value settled Pstryk billing-meter intervals; never fall back to inverter data."""
    flow_rows: list[StepFlows] = []
    steps: list[SimStep] = []
    for itv in series:
        if itv.measured_grid_import_kwh is None or itv.measured_grid_export_kwh is None:
            continue
        if not itv.settled:
            continue
        imp = itv.measured_grid_import_kwh
        exp = itv.measured_grid_export_kwh
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
    if not flow_rows:
        return SimResult(policy="actual_pstryk", steps=steps, cost=None)
    cost = value_flows(
        flow_rows,
        degradation_cost_pln_per_kwh=battery.degradation_cost_pln_per_kwh,
        import_price_adjustment_pln_kwh=battery.import_price_adjustment_pln_kwh,
    )
    return SimResult(policy="actual_pstryk", steps=steps, cost=cost)


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
            + step.battery_to_ev_kwh
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
