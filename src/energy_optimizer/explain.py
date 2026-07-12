"""Deterministic explanations from selected flows and price structure.

The explanation is derived from the integer solution's flows, not from LP duals (which are
advisory only for a MILP). It classifies the next action and produces a human-readable
reason string suitable for the HA ``decision_reason`` sensor and the UI.
"""

from __future__ import annotations

from dataclasses import dataclass

from .optimiser import PlanStepResult

# Small tolerance so tiny numeric residues don't count as "action".
EPS = 1e-3


@dataclass(slots=True)
class Decision:
    action: str  # idle|charge|discharge|export|grid_charge|curtail
    power_kw: float
    target_soc_pct: float
    reason: str


def classify_next_action(
    steps: list[PlanStepResult],
    *,
    buy_price: float | None = None,
    sell_price: float | None = None,
    future_max_buy: float | None = None,
    future_max_sell: float | None = None,
) -> Decision:
    if not steps:
        return Decision("idle", 0.0, 0.0, "No plan available")

    s = steps[0]
    dt_h = s.dt_hours or 0.25

    charge = s.pv_to_battery_kwh + s.grid_to_battery_kwh
    discharge = s.battery_to_load_kwh + s.battery_to_grid_kwh
    curtail = s.curtail_kwh
    export = s.grid_export_kwh

    action, power_kwh = _dominant_action(s, charge, discharge, curtail, export)
    power_kw = power_kwh / dt_h if dt_h else 0.0
    reason = _reason_for(
        action, s, buy_price, sell_price, future_max_buy, future_max_sell
    )
    return Decision(
        action=action,
        power_kw=round(power_kw, 3),
        target_soc_pct=round(s.soc_pct_end, 1),
        reason=reason,
    )


def _dominant_action(
    s: PlanStepResult, charge: float, discharge: float, curtail: float, export: float
) -> tuple[str, float]:
    # Priority: grid_charge > charge > discharge/export > curtail > idle, based on magnitude.
    if s.grid_to_battery_kwh > EPS:
        return "grid_charge", charge
    if charge > EPS and charge >= discharge:
        return "charge", charge
    if s.battery_to_grid_kwh > EPS:
        return "export", discharge
    if discharge > EPS:
        return "discharge", discharge
    if curtail > EPS:
        return "curtail", curtail
    return "idle", 0.0


def _reason_for(
    action: str,
    s: PlanStepResult,
    buy: float | None,
    sell: float | None,
    fut_buy: float | None,
    fut_sell: float | None,
) -> str:
    if action == "grid_charge":
        detail = f" at {buy:.2f} PLN/kWh" if buy is not None else ""
        tail = f"; expected later use above {fut_buy:.2f}" if fut_buy is not None else ""
        return f"Grid-charge{detail}: cheap now vs later{tail}"
    if action == "charge":
        return "Charge from surplus PV: storing energy is worth more than exporting now"
    if action == "export":
        detail = f" at {sell:.2f} PLN/kWh" if sell is not None else ""
        return f"Export from battery{detail}: sell value exceeds hold value plus losses"
    if action == "discharge":
        detail = f" (buy {buy:.2f} PLN/kWh)" if buy is not None else ""
        return f"Discharge to cover load{detail}: cheaper than importing now"
    if action == "curtail":
        return "Curtail PV: export price is non-positive and battery is full"
    reason = "Hold: no profitable action this interval"
    if fut_sell is not None and sell is not None and fut_sell > sell:
        reason = (
            f"Hold charge: later export value {fut_sell:.2f} exceeds current {sell:.2f} plus losses"
        )
    return reason
