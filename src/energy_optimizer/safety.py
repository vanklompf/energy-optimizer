"""Safety rules: plan status plus independent live-control authorization.

Plan statuses (``OK`` / ``LOW_CONFIDENCE`` / ``BLOCKED``) remain for recommendations.
``control_authorized`` is decided separately and is never inferred from planner flags.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from enum import StrEnum
from zoneinfo import ZoneInfo


class Status(StrEnum):
    OK = "ok"
    LOW_CONFIDENCE = "low_confidence"
    BLOCKED = "blocked"


@dataclass(slots=True)
class SafetyInputs:
    telemetry_stale: bool
    telemetry_stale_reasons: list[str]
    have_current_price: bool
    have_pv_forecast: bool
    have_load_forecast: bool
    known_price_hours: float
    horizon_hours: float  # effective planned coverage; recorded for audit, not a gate
    min_price_hours: float = 0.0
    # --- live-control inputs (optional; defaults keep planning-only callers working) ---
    plan_status_ok: bool = True
    plan_age_seconds: float | None = None
    max_plan_age_seconds: float = 900.0
    current_interval_start: dt.datetime | None = None
    current_interval_end: dt.datetime | None = None
    now: dt.datetime | None = None
    current_buy_price: float | None = None
    current_sell_price: float | None = None
    current_price_is_real: bool = False
    current_price_age_seconds: float | None = None
    max_price_age_seconds: float = 3600.0
    telemetry_ages_seconds: dict[str, float | None] = field(default_factory=dict)
    max_telemetry_age_seconds: float = 120.0
    inverter_entities_available: bool = True
    ev_goal_active: bool = False
    ev_telemetry_fresh: bool = True
    lease_held: bool = False
    watchdog_healthy: bool = False
    manual_override_active: bool = False
    recent_command_failures: int = 0
    max_recent_command_failures: int = 2
    soc_pct: float | None = None
    soc_update_age_seconds: float | None = None
    soc_at_boundary: bool = False
    corroborating_power_fresh: bool = True
    battery_control_enabled: bool = False
    mode_is_control: bool = False
    economic_action: bool = False  # charge/export arbitrage; idle/fallback are non-economic


@dataclass(slots=True)
class SafetyReport:
    status: Status
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    control_enabled: bool = False
    control_authorized: bool = False
    control_blockers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "control_enabled": self.control_enabled,
            "control_authorized": self.control_authorized,
            "control_blockers": self.control_blockers,
        }


def evaluate(inputs: SafetyInputs) -> SafetyReport:
    blockers: list[str] = []
    warnings: list[str] = []

    if inputs.telemetry_stale:
        blockers.extend(inputs.telemetry_stale_reasons or ["telemetry stale"])
    if not inputs.have_current_price:
        blockers.append("missing Pstryk price for the current hour")

    if not inputs.have_pv_forecast:
        warnings.append("missing PV forecast; recommendation is low-confidence")
    if not inputs.have_load_forecast:
        warnings.append("missing load forecast; recommendation is low-confidence")
    # Pstryk publishes day-ahead, so forward coverage shrinking through the morning and
    # jumping again after publication is the normal cycle, not a degraded plan. Only an
    # abnormally short window indicates a publication or fetch failure. The acting
    # interval's own price is gated separately in _control_blockers.
    if inputs.known_price_hours < inputs.min_price_hours:
        warnings.append(
            f"only {inputs.known_price_hours:.0f}h of Pstryk prices; "
            f"expected at least {inputs.min_price_hours:.0f}h"
        )

    if blockers:
        status = Status.BLOCKED
    elif warnings:
        status = Status.LOW_CONFIDENCE
    else:
        status = Status.OK

    control_blockers = _control_blockers(inputs, plan_status=status)
    control_authorized = not control_blockers
    return SafetyReport(
        status=status,
        blockers=blockers,
        warnings=warnings,
        control_enabled=inputs.mode_is_control and inputs.battery_control_enabled,
        control_authorized=control_authorized,
        control_blockers=control_blockers,
    )


def _control_blockers(inputs: SafetyInputs, *, plan_status: Status) -> list[str]:
    """Machine-readable blockers for physical battery commands."""
    blockers: list[str] = []

    if not inputs.mode_is_control:
        blockers.append("mode_not_control")
    if not inputs.battery_control_enabled:
        blockers.append("battery_control_disabled")
    if not inputs.lease_held:
        blockers.append("lease_not_held")
    if not inputs.watchdog_healthy:
        blockers.append("watchdog_unhealthy")
    if inputs.manual_override_active:
        blockers.append("manual_override_active")
    if inputs.recent_command_failures > inputs.max_recent_command_failures:
        blockers.append("recent_command_failures")
    if not inputs.inverter_entities_available:
        blockers.append("inverter_entities_unavailable")

    if inputs.economic_action:
        if plan_status != Status.OK or not inputs.plan_status_ok:
            blockers.append("plan_not_ok")
        if inputs.plan_age_seconds is None or inputs.plan_age_seconds > inputs.max_plan_age_seconds:
            blockers.append("plan_stale")
        if not _interval_contains_now(inputs):
            blockers.append("interval_not_current")
        if not inputs.current_price_is_real:
            blockers.append("current_price_not_real")
        if _is_bad_number(inputs.current_buy_price) or _is_bad_number(inputs.current_sell_price):
            blockers.append("current_price_unavailable")
        if (
            inputs.current_price_age_seconds is None
            or inputs.current_price_age_seconds > inputs.max_price_age_seconds
        ):
            blockers.append("current_price_stale")
        for name, age in inputs.telemetry_ages_seconds.items():
            if age is None or age > inputs.max_telemetry_age_seconds:
                blockers.append(f"telemetry_stale:{name}")
        if not _soc_fresh_for_control(inputs):
            blockers.append("soc_not_fresh")
        if inputs.ev_goal_active and not inputs.ev_telemetry_fresh:
            blockers.append("ev_telemetry_stale")
    elif inputs.soc_pct is not None and not _soc_fresh_for_control(inputs):
        # Idle/fallback skips forecast confidence but still rejects unknown SoC.
        blockers.append("soc_not_fresh")

    return blockers


def _interval_contains_now(inputs: SafetyInputs) -> bool:
    if (
        inputs.now is None
        or inputs.current_interval_start is None
        or inputs.current_interval_end is None
    ):
        return False
    return inputs.current_interval_start <= inputs.now < inputs.current_interval_end


def _soc_fresh_for_control(inputs: SafetyInputs) -> bool:
    if inputs.soc_pct is None or _is_bad_number(inputs.soc_pct):
        return False
    if inputs.soc_update_age_seconds is None:
        return False
    if inputs.soc_update_age_seconds > inputs.max_telemetry_age_seconds:
        return False
    # Boundary-pinned SoC still needs corroborating fast telemetry.
    if inputs.soc_at_boundary and not inputs.corroborating_power_fresh:
        return False
    return True


def _is_bad_number(value: float | None) -> bool:
    return value is None or isinstance(value, float) and (math.isnan(value) or math.isinf(value))


def select_current_interval(
    interval_starts: list[dt.datetime],
    *,
    now: dt.datetime,
    step_minutes: int,
    tz: str = "Europe/Warsaw",
) -> tuple[dt.datetime, dt.datetime] | None:
    """Pick the plan interval containing ``now`` (DST-aware via zoneinfo)."""
    if not interval_starts:
        return None
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)
    step = dt.timedelta(minutes=step_minutes)
    for start in interval_starts:
        local_start = start.astimezone(zone)
        local_end = local_start + step
        if local_start <= local_now < local_end:
            return start, start + step
    return None
