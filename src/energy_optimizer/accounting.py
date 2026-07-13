"""Cost / value accounting for plans and simulated series.

Costs are in PLN. Import is charged at the (adjusted) buy price; export earns the sell
price; battery-side throughput carries a degradation cost. The same functions value both
optimiser plans and counterfactual policies so comparisons are apples-to-apples.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(slots=True)
class StepFlows:
    """Per-interval realised or planned flows used for valuation (kWh)."""

    dt_hours: float
    buy_price: float
    sell_price: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_throughput_kwh: float  # sum of charge + discharge on the SoC side


@dataclass(slots=True)
class CostBreakdown:
    import_cost_pln: float
    export_revenue_pln: float
    degradation_cost_pln: float
    net_cost_pln: float
    import_kwh: float
    export_kwh: float
    battery_throughput_kwh: float

    @property
    def battery_cycles(self) -> float:
        # One full cycle = charge + discharge of one capacity's worth of energy. Callers that
        # need a capacity-normalised figure divide throughput by (2 * capacity) themselves.
        return self.battery_throughput_kwh


def value_flows(
    steps: Iterable[StepFlows],
    *,
    degradation_cost_pln_per_kwh: float = 0.0,
    import_price_adjustment_pln_kwh: float = 0.0,
) -> CostBreakdown:
    import_cost = 0.0
    export_rev = 0.0
    deg = 0.0
    imp_kwh = 0.0
    exp_kwh = 0.0
    throughput = 0.0
    for s in steps:
        buy = s.buy_price + import_price_adjustment_pln_kwh
        import_cost += s.grid_import_kwh * buy
        export_rev += s.grid_export_kwh * s.sell_price
        deg += s.battery_throughput_kwh * degradation_cost_pln_per_kwh
        imp_kwh += s.grid_import_kwh
        exp_kwh += s.grid_export_kwh
        throughput += s.battery_throughput_kwh
    return CostBreakdown(
        import_cost_pln=import_cost,
        export_revenue_pln=export_rev,
        degradation_cost_pln=deg,
        net_cost_pln=import_cost - export_rev + deg,
        import_kwh=imp_kwh,
        export_kwh=exp_kwh,
        battery_throughput_kwh=throughput,
    )
