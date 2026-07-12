"""Baseline policies for counterfactual valuation.

A policy maps the current interval state to explicit non-negative SoC-side flows. The
simulator enforces SoC bounds and power limits, then computes grid import/export and
throughput. All flow conventions match the optimiser and the design's dynamics equation.

- ``pv_only``: battery idle; PV serves load, surplus exported, shortfall imported.
- ``self_consumption``: PV serves load, surplus charges battery then exports; shortfall
  is served from battery then grid. The common "prosumer" default.
- ``actual_sigen``: reconstructs flows from measured telemetry (see the simulator).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class IntervalState:
    dt_hours: float
    pv_energy_kwh: float
    load_energy_kwh: float
    buy_price: float
    sell_price: float
    soc_kwh: float
    soc_min_kwh: float
    soc_max_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    eta_charge: float
    eta_discharge: float


@dataclass(slots=True)
class FlowDecision:
    pv_to_load: float = 0.0
    pv_to_battery: float = 0.0  # SoC side
    pv_to_grid: float = 0.0
    curtail: float = 0.0
    grid_to_load: float = 0.0
    grid_to_battery: float = 0.0  # SoC side
    battery_to_load: float = 0.0  # SoC side
    battery_to_grid: float = 0.0  # SoC side


class Policy(Protocol):
    name: str

    def decide(self, state: IntervalState) -> FlowDecision: ...


class PvOnlyPolicy:
    name = "pv_only"

    def decide(self, state: IntervalState) -> FlowDecision:
        pv_to_load = min(state.pv_energy_kwh, state.load_energy_kwh)
        pv_surplus = state.pv_energy_kwh - pv_to_load
        load_deficit = state.load_energy_kwh - pv_to_load
        return FlowDecision(
            pv_to_load=pv_to_load,
            pv_to_grid=pv_surplus,
            grid_to_load=load_deficit,
        )


class SelfConsumptionPolicy:
    name = "self_consumption"

    def decide(self, state: IntervalState) -> FlowDecision:
        d = FlowDecision()
        d.pv_to_load = min(state.pv_energy_kwh, state.load_energy_kwh)
        pv_surplus = state.pv_energy_kwh - d.pv_to_load
        load_deficit = state.load_energy_kwh - d.pv_to_load

        # Charge battery from PV surplus (SoC-side, respecting headroom and power limit).
        charge_headroom = max(0.0, state.soc_max_kwh - state.soc_kwh)
        charge_power_cap = state.max_charge_kw * state.dt_hours
        pv_available_soc = pv_surplus * state.eta_charge
        d.pv_to_battery = min(charge_headroom, charge_power_cap, pv_available_soc)
        pv_consumed_for_charge = d.pv_to_battery / state.eta_charge if state.eta_charge else 0.0
        pv_surplus -= pv_consumed_for_charge
        d.pv_to_grid = max(0.0, pv_surplus)

        # Discharge battery to cover the remaining load (SoC-side).
        discharge_avail = max(0.0, state.soc_kwh - state.soc_min_kwh)
        discharge_power_cap = state.max_discharge_kw * state.dt_hours
        needed_soc = load_deficit / state.eta_discharge if state.eta_discharge else 0.0
        d.battery_to_load = min(discharge_avail, discharge_power_cap, needed_soc)
        load_served = d.battery_to_load * state.eta_discharge
        d.grid_to_load = max(0.0, load_deficit - load_served)
        return d


BASELINE_POLICIES: dict[str, Policy] = {
    PvOnlyPolicy.name: PvOnlyPolicy(),
    SelfConsumptionPolicy.name: SelfConsumptionPolicy(),
}
