"""Fail-safe relay decisions for the fixed-power PHEV charger."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from .config import Settings
from .ev import EV_FULL_TARGET_SOC_PCT

ControlAction = Literal["turn_on", "turn_off", "none"]


@dataclass(frozen=True, slots=True)
class EvLiveState:
    soc_pct: float | None
    charging_status: str | None
    switch_on: bool | None
    switch_last_changed: dt.datetime | None
    power_kw: float | None
    fault: bool


@dataclass(frozen=True, slots=True)
class EvControlDecision:
    desired_on: bool
    action: ControlAction
    reason: str


def decide_ev_control(
    settings: Settings,
    live: EvLiveState,
    *,
    planned_on: bool | None,
    now: dt.datetime,
    force_charge: bool = False,
) -> EvControlDecision:
    """Choose one idempotent relay action.

    Critical safety/availability conditions bypass anti-cycling delays and force OFF.
    Ordinary plan transitions respect minimum on/off times.
    """
    if not settings.ev_control_enabled:
        return EvControlDecision(False, "none", "EV control disabled")
    if live.switch_on is None:
        return EvControlDecision(False, "turn_off", "charger switch state unavailable; forcing OFF")

    forced_off_reason: str | None = None
    if live.fault:
        forced_off_reason = "charger protection fault active"
    elif live.charging_status is None or live.soc_pct is None:
        forced_off_reason = "vehicle telemetry unavailable"
    elif live.charging_status == settings.ev_unplugged_status:
        forced_off_reason = "vehicle unplugged"
    elif live.charging_status not in (
        settings.ev_active_charging_statuses
        if live.switch_on
        else settings.ev_start_charging_statuses
    ):
        forced_off_reason = "vehicle charging status not safe for relay control"
    elif live.soc_pct >= EV_FULL_TARGET_SOC_PCT:
        forced_off_reason = "vehicle target SoC reached"
    elif planned_on is None and not force_charge:
        forced_off_reason = "current optimiser plan unavailable"
    elif live.switch_on and live.switch_last_changed is None:
        forced_off_reason = "energized relay transition timestamp unavailable"
    elif (
        live.switch_on
        and (live.power_kw is None or live.power_kw < settings.ev_min_charging_power_kw)
        and _age_minutes(live.switch_last_changed, now) >= settings.ev_power_start_grace_minutes
    ):
        forced_off_reason = "relay ON but no charging power after startup grace period"

    if forced_off_reason is not None:
        action: ControlAction = "turn_off" if live.switch_on else "none"
        return EvControlDecision(False, action, forced_off_reason)

    if force_charge:
        planned_on = True
    assert planned_on is not None
    if planned_on:
        if live.switch_on:
            reason = (
                "immediate charging override active"
                if force_charge
                else "planned charging slot active"
            )
            return EvControlDecision(True, "none", reason)
        if _age_minutes(live.switch_last_changed, now) < settings.ev_min_off_minutes:
            return EvControlDecision(True, "none", "waiting for minimum off time")
        reason = (
            "immediate charging override requested"
            if force_charge
            else "optimiser selected current charging slot"
        )
        return EvControlDecision(True, "turn_on", reason)

    if not live.switch_on:
        return EvControlDecision(False, "none", "optimiser deferred charging")
    if _age_minutes(live.switch_last_changed, now) < settings.ev_min_on_minutes:
        return EvControlDecision(False, "none", "waiting for minimum on time")
    return EvControlDecision(False, "turn_off", "optimiser deferred charging")


def _age_minutes(changed: dt.datetime | None, now: dt.datetime) -> float:
    if changed is None:
        return 0.0
    if changed.tzinfo is None:
        changed = changed.replace(tzinfo=dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    return max(0.0, (now - changed).total_seconds() / 60.0)
