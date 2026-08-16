"""Fail-safe relay decisions for the fixed-power PHEV charger."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Literal

from .config import Settings
from .ev import EV_FULL_TARGET_SOC_PCT
from .ha_client import HaClient, HaState
from .store import EvControlStatus, EvTelemetry

logger = logging.getLogger(__name__)

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

def ev_live_state(ev: EvTelemetry | None, now: dt.datetime) -> EvLiveState:
    if ev is None:
        return EvLiveState(None, None, None, None, None, False)
    fresh = (now - _aware(ev.ts)).total_seconds() <= 10 * 60 and not ev.stale
    if not fresh:
        # An old database row cannot establish the relay's current physical state.
        return EvLiveState(None, None, None, None, None, bool(ev.fault))
    return EvLiveState(
        soc_pct=ev.soc_pct,
        charging_status=ev.charging_status,
        switch_on=ev.switch_on,
        switch_last_changed=ev.switch_changed,
        power_kw=ev.power_kw,
        fault=ev.fault,
    )


def relay_failure_backoff_decision(
    candidate: EvControlDecision,
    previous: EvControlStatus | None,
    now: dt.datetime,
    backoff_minutes: int,
) -> EvControlDecision:
    """Prevent repeated ON pulses after an ambiguous activation attempt."""
    if not candidate.desired_on or previous is None:
        return candidate
    previous_reason = previous.reason or ""
    previous_was_failed_on = (
        "CRITICAL:" in previous_reason and "turn_on" in previous_reason
    ) or "relay retry backoff" in previous_reason
    if not previous_was_failed_on:
        return candidate
    elapsed = max(0.0, (now - _aware(previous.ts)).total_seconds())
    remaining_seconds = backoff_minutes * 60 - elapsed
    if remaining_seconds <= 0:
        return candidate
    remaining_minutes = max(1, math.ceil(remaining_seconds / 60))
    if "could not be confirmed" in previous_reason:
        return EvControlDecision(
            False,
            "turn_off",
            "CRITICAL: relay retry backoff active and physical OFF remains unconfirmed; "
            f"forcing OFF ({remaining_minutes} minutes remaining)",
        )
    return EvControlDecision(
        False,
        "none",
        "relay retry backoff after turn_on verification failure; "
        f"OFF was confirmed, retry allowed in {remaining_minutes} minutes",
    )


async def apply_ev_relay_decision(
    ha: HaClient,
    entity_id: str,
    decision: EvControlDecision,
    *,
    settle_seconds: float = 5.0,
    verify_interval_seconds: float = 2.0,
    verify_timeout_seconds: float = 30.0,
) -> EvControlDecision:
    """Actuate and verify a relay decision; every ambiguous outcome forces OFF."""
    if decision.action == "none":
        return decision

    try:
        await ha.call_service("switch", decision.action, {"entity_id": entity_id})
        if await verify_ev_relay_state(
            ha,
            entity_id,
            decision.desired_on,
            settle_seconds=settle_seconds,
            interval_seconds=verify_interval_seconds,
            timeout_seconds=verify_timeout_seconds,
        ):
            return decision
        failure = f"{decision.action} actuation verification failed"
    except Exception as exc:  # timeout may follow a successful physical action
        logger.exception("EV relay %s outcome is ambiguous", decision.action)
        failure = f"{decision.action} outcome ambiguous ({type(exc).__name__})"

    return await force_ev_relay_off(
        ha,
        entity_id,
        failure,
        settle_seconds=settle_seconds,
        verify_interval_seconds=verify_interval_seconds,
        verify_timeout_seconds=verify_timeout_seconds,
    )


async def force_ev_relay_off(
    ha: HaClient,
    entity_id: str,
    failure: str,
    *,
    settle_seconds: float,
    verify_interval_seconds: float,
    verify_timeout_seconds: float,
) -> EvControlDecision:
    errors: list[str] = []
    try:
        await ha.call_service("switch", "turn_off", {"entity_id": entity_id})
    except Exception as exc:
        logger.exception("EV emergency turn_off service call failed")
        errors.append(f"turn_off error {type(exc).__name__}")

    off_confirmed = await verify_ev_relay_state(
        ha,
        entity_id,
        False,
        settle_seconds=settle_seconds,
        interval_seconds=verify_interval_seconds,
        timeout_seconds=verify_timeout_seconds,
    )
    if off_confirmed:
        suffix = "forced OFF confirmed"
    else:
        suffix = "forced OFF could not be confirmed"
        if errors:
            suffix += f" ({', '.join(errors)})"
    alarm = f"CRITICAL: {failure}; {suffix}"
    logger.error("EV charger actuation alarm: %s", alarm)
    return EvControlDecision(False, "turn_off", alarm)


async def verify_ev_relay_state(
    ha: HaClient,
    entity_id: str,
    expected_on: bool,
    *,
    settle_seconds: float,
    interval_seconds: float,
    timeout_seconds: float,
) -> bool:
    """Allow HA time to observe the relay, retrying stale or failed readbacks."""
    attempts = max(1, int((timeout_seconds - settle_seconds) // interval_seconds) + 1)
    for attempt in range(1, attempts + 1):
        await asyncio.sleep(settle_seconds if attempt == 1 else interval_seconds)
        try:
            actual = state_bool(await ha.get_state(entity_id))
        except Exception as exc:
            logger.warning(
                "EV relay readback failed (attempt %d/%d): %s",
                attempt,
                attempts,
                exc,
            )
            continue
        if actual is expected_on:
            return True
        logger.warning(
            "EV relay state not yet %s (attempt %d/%d)",
            "ON" if expected_on else "OFF",
            attempt,
            attempts,
        )
    return False


def ev_fault_status(states: list[HaState | None]) -> tuple[bool, bool]:
    """Treat any missing/unavailable configured protection signal as a fault."""
    values = [state_bool(state) for state in states]
    unavailable = any(value is None for value in values)
    return (unavailable or any(value is True for value in values), unavailable)


def state_bool(state: HaState | None) -> bool | None:
    if state is None:
        return None
    value = state.state.lower()
    if value == "on":
        return True
    if value == "off":
        return False
    return None


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
