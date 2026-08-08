"""Duration-aware explicit-flow MILP optimiser (HiGHS via PuLP).

Rolling-horizon plan over aligned steps. Each interval carries ``dt_hours`` and all
equations operate on interval *energies* (kWh), never mixed kW/kWh. Explicit source/dest
flows make the battery-export and grid-charge feature flags exact. Two binaries per
interval (``is_charging``, ``is_importing``) forbid simultaneous charge/discharge and
simultaneous import/export, which is essential once prices can be negative.

All battery flow variables (``pv_to_battery``, ``grid_to_battery``, ``battery_to_load``,
``battery_to_grid``) are measured on the SoC side, exactly as in the design's dynamics
equation. Conversion losses appear where energy crosses the battery/grid boundary.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import pulp

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IntervalInput:
    """One optimisation interval. Prices in PLN/kWh; energies in kWh."""

    interval_start: str  # ISO timestamp (opaque to the solver)
    dt_hours: float
    pv_energy_kwh: float
    load_energy_kwh: float
    buy_price: float  # full import price incl. distribution etc.
    sell_price: float
    price_is_real: bool = True  # False for padded/forecast prices
    ev_available: bool = False
    ev_required_soon: bool = False
    ev_opportunistic_allowed: bool = True


@dataclass(slots=True)
class OptimiserParams:
    battery_capacity_kwh: float
    soc_min_kwh: float
    battery_hard_min_kwh: float
    soc_max_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    eta_charge: float
    eta_discharge: float
    site_import_limit_kw: float
    site_export_limit_kw: float
    inverter_limit_kw: float | None
    degradation_cost_pln_per_kwh: float
    import_price_adjustment_pln_kwh: float = 0.0
    allow_battery_export: bool = True
    allow_grid_charging: bool = True
    terminal_soc_salvage_pln_kwh: float = 0.0
    preserve_terminal_soc: bool = False
    ev_charge_power_kw: float = 0.0
    ev_target_slots: int = 0
    ev_minimum_slots: int = 0
    ev_early_start_value_pln_per_hour: float = 10.0
    ev_opportunistic_terminal_soc_kwh: float = 0.0


@dataclass(slots=True)
class PlanStepResult:
    interval_start: str
    dt_hours: float
    pv_to_load_kwh: float
    pv_to_ev_kwh: float
    pv_to_battery_kwh: float
    pv_to_grid_kwh: float
    grid_to_load_kwh: float
    grid_to_ev_kwh: float
    grid_to_battery_kwh: float
    battery_to_load_kwh: float
    battery_to_grid_kwh: float
    battery_to_ev_kwh: float
    curtail_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    soc_kwh_end: float
    soc_pct_end: float
    ev_charge_kwh: float = 0.0
    ev_minimum_charge_kwh: float = 0.0
    ev_opportunistic_charge_kwh: float = 0.0
    marginal_value: float | None = None


@dataclass(slots=True)
class OptimiseResult:
    status: str  # "optimal" | "infeasible" | "error"
    objective_pln: float | None
    steps: list[PlanStepResult] = field(default_factory=list)
    solve_ms: float = 0.0
    solver: str = "unknown"


def optimise(
    intervals: list[IntervalInput],
    soc_start_kwh: float,
    params: OptimiserParams,
    *,
    solver: pulp.LpSolver | None = None,
    msg: bool = False,
) -> OptimiseResult:
    """Solve the rolling-horizon MILP. Returns a plan and metadata."""
    if not intervals:
        return OptimiseResult(status="error", objective_pln=None, solver="none")

    t0 = time.perf_counter()
    n = len(intervals)
    prob = pulp.LpProblem("energy_optimizer", pulp.LpMinimize)

    def var(name: str, i: int, up: float | None = None) -> pulp.LpVariable:
        return pulp.LpVariable(f"{name}_{i}", lowBound=0, upBound=up)

    pv_to_load, pv_to_batt, pv_to_grid, pv_to_ev, curtail = [], [], [], [], []
    grid_to_load, grid_to_batt, grid_to_ev = [], [], []
    batt_to_load, batt_to_grid = [], []
    batt_to_ev_minimum, batt_to_ev_opportunistic = [], []
    grid_import, grid_export = [], []
    soc = []  # soc[i] = SoC at END of interval i
    reserve_shortfall, is_below_reserve = [], []
    is_charging, is_importing = [], []
    is_ev_minimum, is_ev_opportunistic = [], []

    for i, itv in enumerate(intervals):
        pv_to_load.append(var("pv_to_load", i))
        pv_to_batt.append(var("pv_to_batt", i))
        pv_to_grid.append(var("pv_to_grid", i))
        pv_to_ev.append(var("pv_to_ev", i))
        curtail.append(var("curtail", i))
        grid_to_load.append(var("grid_to_load", i))
        grid_to_ev.append(var("grid_to_ev", i))
        grid_to_batt.append(
            var("grid_to_batt", i) if params.allow_grid_charging else _zero(i, "g2b")
        )
        batt_to_load.append(var("batt_to_load", i))
        batt_to_grid.append(
            var("batt_to_grid", i) if params.allow_battery_export else _zero(i, "b2g")
        )
        batt_to_ev_minimum.append(var("batt_to_ev_minimum", i))
        batt_to_ev_opportunistic.append(var("batt_to_ev_opportunistic", i))
        grid_import.append(var("grid_import", i))
        grid_export.append(var("grid_export", i))
        soc.append(
            pulp.LpVariable(
                f"soc_{i}", lowBound=params.battery_hard_min_kwh, upBound=params.soc_max_kwh
            )
        )
        reserve_shortfall.append(
            pulp.LpVariable(
                f"reserve_shortfall_{i}",
                lowBound=0,
                upBound=max(0.0, params.soc_min_kwh - params.battery_hard_min_kwh),
            )
        )
        is_below_reserve.append(pulp.LpVariable(f"is_below_reserve_{i}", cat="Binary"))
        is_charging.append(pulp.LpVariable(f"is_charging_{i}", cat="Binary"))
        is_importing.append(pulp.LpVariable(f"is_importing_{i}", cat="Binary"))
        is_ev_minimum.append(
            pulp.LpVariable(f"is_ev_charging_{i}", cat="Binary")
            if (
                itv.ev_available
                and itv.ev_required_soon
                and params.ev_charge_power_kw > 0
                and params.ev_minimum_slots > 0
            )
            else _zero(i, "ev")
        )
        is_ev_opportunistic.append(
            pulp.LpVariable(f"is_ev_opportunistic_{i}", cat="Binary")
            if (
                itv.ev_available
                and itv.ev_opportunistic_allowed
                and params.ev_charge_power_kw > 0
                and params.ev_target_slots > params.ev_minimum_slots
            )
            else _zero(i, "ev_opp")
        )

    # Objective: import cost - export revenue + degradation on battery-side throughput.
    obj_terms = []
    for i, itv in enumerate(intervals):
        buy = itv.buy_price + params.import_price_adjustment_pln_kwh
        obj_terms.append(grid_import[i] * buy)
        obj_terms.append(-grid_export[i] * itv.sell_price)
        batt_to_ev = batt_to_ev_minimum[i] + batt_to_ev_opportunistic[i]
        throughput = (
            pv_to_batt[i] + grid_to_batt[i] + batt_to_load[i] + batt_to_grid[i] + batt_to_ev
        )
        obj_terms.append(throughput * params.degradation_cost_pln_per_kwh)
        # User preference: once forecast-backed, opportunistic EV energy is front-loaded.
        obj_terms.append(
            is_ev_opportunistic[i] * i * itv.dt_hours * params.ev_early_start_value_pln_per_hour
        )
    if params.terminal_soc_salvage_pln_kwh and not params.preserve_terminal_soc:
        obj_terms.append(-soc[n - 1] * params.terminal_soc_salvage_pln_kwh)
    prob += pulp.lpSum(obj_terms)

    for i, itv in enumerate(intervals):
        dt_h = itv.dt_hours
        # PV allocation (charge measured on SoC side => divide by eta_c to get PV consumed).
        prob += (
            (
                pv_to_load[i]
                + pv_to_batt[i] / params.eta_charge
                + pv_to_grid[i]
                + pv_to_ev[i]
                + curtail[i]
                == itv.pv_energy_kwh
            ),
            f"pv_alloc_{i}",
        )
        # Load supply (battery delivers battery_to_load * eta_d to the load).
        prob += (
            (
                pv_to_load[i] + grid_to_load[i] + batt_to_load[i] * params.eta_discharge
                == itv.load_energy_kwh
            ),
            f"house_load_supply_{i}",
        )
        prob += (
            (
                pv_to_ev[i]
                + grid_to_ev[i]
                + (batt_to_ev_minimum[i] + batt_to_ev_opportunistic[i]) * params.eta_discharge
                == (is_ev_minimum[i] + is_ev_opportunistic[i])
                * params.ev_charge_power_kw
                * itv.dt_hours
            ),
            f"ev_supply_{i}",
        )
        prob += is_ev_minimum[i] + is_ev_opportunistic[i] <= 1, f"ev_mode_{i}"
        prob += (
            batt_to_ev_minimum[i] <= params.max_discharge_kw * dt_h * is_ev_minimum[i],
            f"battery_to_ev_minimum_mode_{i}",
        )
        prob += (
            batt_to_ev_opportunistic[i] <= params.max_discharge_kw * dt_h * is_ev_opportunistic[i],
            f"battery_to_ev_opportunistic_mode_{i}",
        )
        # Guaranteed pre-departure energy may use grid. During a normal opportunistic
        # slot the complete site import must be zero: PV plus stationary battery above
        # reserve must cover both the house and EV.
        prob += (
            (
                grid_import[i]
                <= params.site_import_limit_kw * itv.dt_hours * (1 - is_ev_opportunistic[i])
            ),
            f"ev_no_grid_import_{i}",
        )
        # Grid import/export composition.
        prob += (
            (
                grid_import[i]
                == grid_to_load[i] + grid_to_ev[i] + grid_to_batt[i] / params.eta_charge
            ),
            f"grid_import_{i}",
        )
        prob += (
            (grid_export[i] == pv_to_grid[i] + batt_to_grid[i] * params.eta_discharge),
            f"grid_export_{i}",
        )
        # Battery dynamics.
        prev = soc_start_kwh if i == 0 else soc[i - 1]
        prob += (
            (
                soc[i]
                == prev
                + pv_to_batt[i]
                + grid_to_batt[i]
                - batt_to_load[i]
                - batt_to_grid[i]
                - batt_to_ev_minimum[i]
                - batt_to_ev_opportunistic[i]
            ),
            f"soc_dyn_{i}",
        )
        reserve_gap = params.soc_min_kwh - soc[i]
        reserve_big_m = params.soc_max_kwh - params.battery_hard_min_kwh
        prob += reserve_shortfall[i] >= reserve_gap, f"reserve_shortfall_lb_{i}"
        prob += (
            reserve_shortfall[i] <= reserve_gap + reserve_big_m * (1 - is_below_reserve[i]),
            f"reserve_shortfall_exact_{i}",
        )
        prob += (
            reserve_shortfall[i] <= reserve_big_m * is_below_reserve[i],
            f"reserve_shortfall_zero_above_{i}",
        )
        prob += reserve_gap <= reserve_big_m * is_below_reserve[i], f"below_reserve_mode_{i}"
        reserve_shortfall_prev = (
            max(0.0, params.soc_min_kwh - soc_start_kwh) if i == 0 else reserve_shortfall[i - 1]
        )
        prob += (
            reserve_shortfall[i] <= reserve_shortfall_prev + batt_to_ev_minimum[i],
            f"reserve_shortfall_growth_{i}",
        )
        # Charge / discharge power limits (SoC side) gated by is_charging.
        prob += (
            (pv_to_batt[i] + grid_to_batt[i] <= params.max_charge_kw * dt_h * is_charging[i]),
            f"charge_lim_{i}",
        )
        prob += (
            (
                batt_to_load[i]
                + batt_to_grid[i]
                + batt_to_ev_minimum[i]
                + batt_to_ev_opportunistic[i]
                <= params.max_discharge_kw * dt_h * (1 - is_charging[i])
            ),
            f"discharge_lim_{i}",
        )
        # Grid direction limits gated by is_importing.
        prob += (
            (grid_import[i] <= params.site_import_limit_kw * dt_h * is_importing[i]),
            f"import_lim_{i}",
        )
        prob += (
            (grid_export[i] <= params.site_export_limit_kw * dt_h * (1 - is_importing[i])),
            f"export_lim_{i}",
        )
        # Optional combined inverter throughput limit.
        if params.inverter_limit_kw is not None:
            cap = params.inverter_limit_kw * dt_h
            prob += grid_import[i] + grid_export[i] <= cap, f"inverter_lim_{i}"

    if params.preserve_terminal_soc:
        prob += soc[n - 1] >= soc_start_kwh, "terminal_preserve"

    prob += pulp.lpSum(is_ev_minimum) == params.ev_minimum_slots, "ev_minimum_slots"
    prob += (
        (
            pulp.lpSum(is_ev_opportunistic)
            == max(0, params.ev_target_slots - params.ev_minimum_slots)
        ),
        "ev_opportunistic_slots",
    )

    # Forecast-backed opportunistic charging is permitted only when the same plan
    # still fills the stationary battery to its configured target. Do not let the
    # solver satisfy that terminal condition by importing energy into the battery.
    if (
        params.ev_opportunistic_terminal_soc_kwh > 0
        and params.ev_target_slots > params.ev_minimum_slots
    ):
        prob += (
            soc[n - 1] >= params.ev_opportunistic_terminal_soc_kwh,
            "ev_battery_filled_later",
        )
        for i in range(n):
            prob += grid_to_batt[i] == 0, f"ev_no_grid_battery_recovery_{i}"

    chosen_solver = solver or _default_solver(msg=msg)
    try:
        prob.solve(chosen_solver)
    except pulp.PulpSolverError as exc:  # pragma: no cover - solver env issue
        logger.error("Solver error: %s", exc)
        return OptimiseResult(
            status="error", objective_pln=None, solve_ms=_ms(t0), solver=_solver_name(chosen_solver)
        )

    status = pulp.LpStatus[prob.status]
    solve_ms = _ms(t0)
    if status != "Optimal":
        return OptimiseResult(
            status="infeasible" if status == "Infeasible" else "error",
            objective_pln=None,
            solve_ms=solve_ms,
            solver=_solver_name(chosen_solver),
        )

    steps: list[PlanStepResult] = []
    for i, itv in enumerate(intervals):
        soc_end = _val(soc[i])
        steps.append(
            PlanStepResult(
                interval_start=itv.interval_start,
                dt_hours=itv.dt_hours,
                pv_to_load_kwh=_val(pv_to_load[i]),
                pv_to_ev_kwh=_val(pv_to_ev[i]),
                pv_to_battery_kwh=_val(pv_to_batt[i]),
                pv_to_grid_kwh=_val(pv_to_grid[i]),
                grid_to_load_kwh=_val(grid_to_load[i]),
                grid_to_ev_kwh=_val(grid_to_ev[i]),
                grid_to_battery_kwh=_val(grid_to_batt[i]),
                battery_to_load_kwh=_val(batt_to_load[i]),
                battery_to_grid_kwh=_val(batt_to_grid[i]),
                battery_to_ev_kwh=(_val(batt_to_ev_minimum[i]) + _val(batt_to_ev_opportunistic[i])),
                curtail_kwh=_val(curtail[i]),
                grid_import_kwh=_val(grid_import[i]),
                grid_export_kwh=_val(grid_export[i]),
                soc_kwh_end=soc_end,
                soc_pct_end=100.0 * soc_end / params.battery_capacity_kwh
                if params.battery_capacity_kwh
                else 0.0,
                ev_charge_kwh=(_val(is_ev_minimum[i]) + _val(is_ev_opportunistic[i]))
                * params.ev_charge_power_kw
                * itv.dt_hours,
                ev_minimum_charge_kwh=_val(is_ev_minimum[i])
                * params.ev_charge_power_kw
                * itv.dt_hours,
                ev_opportunistic_charge_kwh=_val(is_ev_opportunistic[i])
                * params.ev_charge_power_kw
                * itv.dt_hours,
            )
        )

    return OptimiseResult(
        status="optimal",
        objective_pln=pulp.value(prob.objective),
        steps=steps,
        solve_ms=solve_ms,
        solver=_solver_name(chosen_solver),
    )


def _default_solver(msg: bool = False) -> pulp.LpSolver:
    """Prefer HiGHS if available, otherwise fall back to CBC (bundled with PuLP)."""
    for factory in (
        lambda: pulp.HiGHS(msg=msg),  # highspy-based (preferred)
        lambda: pulp.HiGHS_CMD(msg=msg),  # external highs binary
    ):
        try:
            solver = factory()
            if solver.available():
                return solver
        except (AttributeError, pulp.PulpSolverError):
            continue
    return pulp.PULP_CBC_CMD(msg=msg)


def _solver_name(solver: pulp.LpSolver) -> str:
    return type(solver).__name__


def _zero(i: int, tag: str) -> pulp.LpVariable:
    """A variable pinned to zero, used to disable a flow via feature flag."""
    return pulp.LpVariable(f"{tag}_zero_{i}", lowBound=0, upBound=0)


def _val(v: pulp.LpVariable) -> float:
    x = v.value()
    return round(float(x), 6) if x is not None else 0.0


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 3)
