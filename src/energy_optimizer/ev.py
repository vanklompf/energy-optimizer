"""EV/PHEV flexible-load planning helpers."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .config import Settings
from .safety import SafetyReport, Status

EV_FULL_TARGET_SOC_PCT = 100.0


@dataclass(frozen=True, slots=True)
class EvRequirements:
    departure_at: dt.datetime
    minimum_slots: int
    target_slots: int
    minimum_shortfall_slots: int = 0
    target_shortfall_slots: int = 0


def same_day_forecast_surplus_kwh(
    now: dt.datetime,
    pv_by_start: dict[dt.datetime, float],
    load_by_start: dict[dt.datetime, float],
    *,
    tz: str,
    factor: float,
) -> float:
    """Return conservative future PV surplus remaining in the current local day."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    total = 0.0
    for start, pv_kwh in pv_by_start.items():
        aware_start = start if start.tzinfo else start.replace(tzinfo=dt.UTC)
        if aware_start < now or aware_start.astimezone(zone).date() != today:
            continue
        total += max(0.0, pv_kwh - load_by_start.get(start, 0.0))
    return total * max(0.0, min(1.0, factor))


def build_ev_requirements(
    soc_pct: float,
    now: dt.datetime,
    interval_starts: list[dt.datetime],
    settings: Settings,
    *,
    forecast_surplus_kwh: float | None = None,
    max_opportunistic_slots: int | None = None,
) -> EvRequirements:
    """Convert vehicle SoC targets into fixed-power charging slots.

    The minimum target is due by the next configured departure hour. The full target
    may use any remaining interval in the rolling horizon, allowing the optimiser to
    prefer forecast solar or the least expensive grid hours.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    local_now = now.astimezone(ZoneInfo(settings.tz))
    departure_local = local_now.replace(
        hour=settings.ev_departure_hour, minute=0, second=0, microsecond=0
    )
    if departure_local <= local_now:
        departure_local += dt.timedelta(days=1)
    departure_at = departure_local.astimezone(dt.UTC)

    slot_kwh = settings.ev_charge_power_kw * settings.step_hours
    if slot_kwh <= 0:
        return EvRequirements(departure_at, 0, 0)

    bounded_soc = max(0.0, min(100.0, soc_pct))

    def slots_for(target_pct: float) -> int:
        battery_deficit = settings.ev_capacity_kwh * max(target_pct - bounded_soc, 0.0) / 100.0
        ac_energy = battery_deficit / settings.ev_charge_efficiency
        return math.ceil(ac_energy / slot_kwh - 1e-12)

    available = len(interval_starts)
    available_soon = sum(1 for start in interval_starts if start < departure_at)
    target_required = slots_for(EV_FULL_TARGET_SOC_PCT)
    minimum_required = slots_for(settings.ev_minimum_target_soc_pct)
    guaranteed_slots = min(minimum_required, available_soon)
    if forecast_surplus_kwh is None:
        target_slots = min(target_required, available)
    else:
        opportunistic_slots = max(0, math.floor(forecast_surplus_kwh / slot_kwh + 1e-12))
        if max_opportunistic_slots is not None:
            opportunistic_slots = min(opportunistic_slots, max(0, max_opportunistic_slots))
        target_slots = min(target_required, available, guaranteed_slots + opportunistic_slots)
    minimum_slots = min(guaranteed_slots, target_slots)
    return EvRequirements(
        departure_at,
        minimum_slots,
        target_slots,
        minimum_shortfall_slots=max(0, minimum_required - available_soon),
        target_shortfall_slots=max(0, target_required - target_slots),
    )


def apply_ev_shortfall_warning(
    report: SafetyReport, requirements: EvRequirements, step_minutes: int
) -> None:
    """Expose an infeasible departure target while retaining charge-now fallback slots."""
    shortfall = requirements.minimum_shortfall_slots
    if shortfall <= 0:
        return
    report.warnings.append(
        f"EV departure target infeasible by {shortfall} slots "
        f"({shortfall * step_minutes} minutes); charging every available pre-departure slot"
    )
    if report.status == Status.OK:
        report.status = Status.LOW_CONFIDENCE
