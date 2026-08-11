"""Transactional Sigenergy adapter over Home Assistant control primitives.

Converts an authorized ``BatteryControlIntent`` into an ordered, idempotent, reversible
Remote EMS transaction. Primitives come from ``HaClient``; this module owns mode ordering
and physical verification against the verified control contract.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from .battery_control import (
    BatteryControlIntent,
    ControlDirection,
    ControllerState,
    ControlResult,
    require_neutral_before_reversal,
)
from .config import Settings
from .ha_client import (
    ENTITY_BATTERY_POWER,
    ENTITY_GRID_EXPORT_POWER,
    ENTITY_GRID_IMPORT_POWER,
    ENTITY_SOC,
    AckResult,
    AckStatus,
    HaClient,
)

logger = logging.getLogger(__name__)

_STABLE_GOOD_SAMPLES = 3


@dataclass(slots=True)
class PhysicalSnapshot:
    battery_power_kw: float | None
    grid_import_kw: float | None
    grid_export_kw: float | None
    soc_pct: float | None
    ems_mode: str | None = None
    sampled_at: dt.datetime | None = None
    battery_power_updated_at: dt.datetime | None = None
    grid_import_updated_at: dt.datetime | None = None
    grid_export_updated_at: dt.datetime | None = None
    soc_updated_at: dt.datetime | None = None
    charge_limit_kw: float | None = None
    discharge_limit_kw: float | None = None
    charge_cutoff_pct: float | None = None
    discharge_cutoff_pct: float | None = None


class PhysicalReader(Protocol):
    async def read_physical(self) -> PhysicalSnapshot: ...


@dataclass
class SigenergyControlResult:
    control: ControlResult
    service_calls: list[tuple[str, str, dict]] = field(default_factory=list)
    ack_results: list[AckResult] = field(default_factory=list)
    lockout: bool = False


class SigenergyController:
    """Ordered Remote EMS transactions. Discharge/export remain capability-blocked."""

    def __init__(
        self,
        ha: HaClient,
        settings: Settings,
        *,
        physical: PhysicalReader | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ha = ha
        self._settings = settings
        self._physical = physical
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_direction = ControlDirection.IDLE
        self._service_calls: list[tuple[str, str, dict]] = []
        self._ack_results: list[AckResult] = []
        self._consecutive_failures = 0

    @property
    def remote_switch(self) -> str:
        return self._settings.battery_control_remote_ems_switch_entity

    @property
    def mode_select(self) -> str:
        return self._settings.battery_control_mode_select_entity

    async def apply_intent(
        self,
        intent: BatteryControlIntent,
        *,
        previous_direction: ControlDirection | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> SigenergyControlResult:
        self._service_calls = []
        self._ack_results = []
        command_id = str(uuid.uuid4())
        t0 = time.perf_counter()
        prev = previous_direction if previous_direction is not None else self._last_direction

        if intent.direction == ControlDirection.FALLBACK:
            return await self.fallback("intent_fallback", command_id=command_id)

        if intent.direction == ControlDirection.DISCHARGE or intent.export:
            if not (
                self._settings.battery_control_authorize_discharge
                and "DISCHARGE" in self._settings.battery_control_supported_directions
            ):
                return self._fail(
                    command_id,
                    ControllerState.ARMED_IDLE,
                    "discharge_export_not_authorized",
                    t0,
                    lockout=False,
                )

        if intent.direction == ControlDirection.CHARGE and intent.grid_charge:
            if intent.grid_charge and not self._settings.battery_control_grid_charge_enabled:
                return self._fail(
                    command_id,
                    ControllerState.ARMED_IDLE,
                    "grid_charge_not_enabled",
                    t0,
                )

        physical = await self._read_physical()
        if physical.battery_power_kw is None:
            return self._fail(
                command_id, ControllerState.LOCKOUT, "physical_state_unknown", t0, lockout=True
            )

        # Direction reversal must pass through verified Standby/neutral.
        if require_neutral_before_reversal(prev, intent.direction):
            neutral = await self._enter_standby_neutral(cancel_event=cancel_event)
            if neutral.status not in {AckStatus.ACKNOWLEDGED, AckStatus.IDEMPOTENT_NOOP}:
                return await self._failure_fallback(
                    command_id, "reversal_neutral_failed", t0, cancel_event
                )

        if intent.direction == ControlDirection.IDLE:
            result = await self._ensure_idle_local(cancel_event=cancel_event)
            self._last_direction = ControlDirection.IDLE
            return result

        # 1-3: Remote EMS off path — dormant Standby then configured limits.
        remote = await self._ha.get_state(self.remote_switch)
        if remote is None or remote.state in {"unknown", "unavailable", None}:
            return self._fail(
                command_id, ControllerState.LOCKOUT, "remote_switch_unavailable", t0, lockout=True
            )

        if remote.state == "off":
            standby = await self._tracked_select(
                self.mode_select,
                self._settings.battery_control_mode_standby,
                cancel_event=cancel_event,
            )
            if standby.status not in {AckStatus.ACKNOWLEDGED, AckStatus.IDEMPOTENT_NOOP}:
                return self._fail(
                    command_id,
                    ControllerState.LOCKOUT,
                    "dormant_standby_unacknowledged",
                    t0,
                    lockout=True,
                )
            limits = await self._write_configured_limits(cancel_event=cancel_event)
            if limits is not None:
                return limits

            enable = await self._tracked_switch(self.remote_switch, True, cancel_event=cancel_event)
            if enable.status not in {AckStatus.ACKNOWLEDGED, AckStatus.IDEMPOTENT_NOOP}:
                return await self._failure_fallback(
                    command_id, "remote_ems_enable_failed", t0, cancel_event
                )

            # Require Standby while Remote EMS is on before commanding.
            standby_on = await self._tracked_select(
                self.mode_select,
                self._settings.battery_control_mode_standby,
                cancel_event=cancel_event,
            )
            if standby_on.status not in {AckStatus.ACKNOWLEDGED, AckStatus.IDEMPOTENT_NOOP}:
                return await self._failure_fallback(
                    command_id, "standby_after_enable_failed", t0, cancel_event
                )
        else:
            # Already in Remote EMS — still refuse to continue without reliable limit ack path.
            if not self._ha.number_register_ack_reliable:
                return self._fail(
                    command_id,
                    ControllerState.ARMED_IDLE,
                    "UNACKNOWLEDGED_LIMIT",
                    t0,
                )

        if not await self._wait_neutral(cancel_event=cancel_event):
            return await self._failure_fallback(
                command_id, "standby_physical_timeout", t0, cancel_event
            )

        if intent.direction != ControlDirection.CHARGE:
            return self._fail(
                command_id,
                ControllerState.ARMED_IDLE,
                "command_mode_not_characterized",
                t0,
            )

        mode = self._settings.battery_control_command_mode
        # Only the characterized charge path may proceed.
        if mode != self._settings.battery_control_mode_charge_grid_first:
            return self._fail(
                command_id, ControllerState.ARMED_IDLE, "command_mode_not_characterized", t0
            )

        # Write charge limit for this command from explicit config/intent power.
        charge_limit = min(
            intent.requested_power_kw,
            self._settings.battery_control_max_charge_kw,
        )
        if self._ha.number_register_ack_reliable:
            ack = await self._tracked_number(
                self._settings.battery_control_charge_limit_entity,
                charge_limit,
                cancel_event=cancel_event,
            )
            if ack.status not in {
                AckStatus.ACKNOWLEDGED,
                AckStatus.IDEMPOTENT_NOOP,
                AckStatus.VALUE_COERCED,
            }:
                return await self._failure_fallback(
                    command_id, "charge_limit_unacknowledged", t0, cancel_event
                )
        else:
            return self._fail(command_id, ControllerState.ARMED_IDLE, "UNACKNOWLEDGED_LIMIT", t0)

        selected = await self._tracked_select(self.mode_select, mode, cancel_event=cancel_event)
        if selected.status not in {AckStatus.ACKNOWLEDGED, AckStatus.IDEMPOTENT_NOOP}:
            return await self._failure_fallback(
                command_id, "command_mode_unacknowledged", t0, cancel_event
            )

        command_started = dt.datetime.now(tz=dt.UTC)
        verified, verify_reason = await self._verify_charge_command(
            intent,
            min_kw=max(0.1, charge_limit * 0.5),
            commanded_mode=mode,
            commanded_charge_limit_kw=charge_limit,
            command_started_at=command_started,
            cancel_event=cancel_event,
        )
        if not verified:
            return await self._failure_fallback(
                command_id, verify_reason or "no_physical_charge_response", t0, cancel_event
            )

        self._consecutive_failures = 0
        self._last_direction = ControlDirection.CHARGE
        return SigenergyControlResult(
            control=ControlResult(
                command_id=command_id,
                requested_state=ControllerState.ACTIVE_CHARGE,
                observed_state=ControllerState.ACTIVE_CHARGE,
                entity_readback={self.mode_select: mode},
                physical_verified=True,
                retries=0,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                failure_reason=None,
                lockout_reason=None,
            ),
            service_calls=list(self._service_calls),
            ack_results=list(self._ack_results),
        )

    async def fallback(
        self,
        reason: str,
        *,
        command_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> SigenergyControlResult:
        """Plan-independent fallback: Standby → restore local limits → Remote EMS off.

        ``physical_verified=True`` requires Standby ack, bounded neutral, reliable limit
        restores, Remote EMS off ack, and observed local behavior. Remote-off alone never
        counts as verified safe.
        """
        self._service_calls = []
        self._ack_results = []
        # Once fallback has begun, a cancelled command must not cancel fail-safe cleanup.
        # Each restoration step is still bounded by its own acknowledgement/physical timeout.
        cancel_event = None
        command_id = command_id or str(uuid.uuid4())
        t0 = time.perf_counter()
        evidence: dict[str, object] = {"attempted": True, "reason": reason}

        standby = await self._tracked_select(
            self.mode_select,
            self._settings.battery_control_fallback_mode,
            cancel_event=cancel_event,
        )
        evidence["standby_ack"] = standby.status.value
        if standby.status not in {AckStatus.ACKNOWLEDGED, AckStatus.IDEMPOTENT_NOOP}:
            return self._fallback_incomplete(
                command_id, reason, "fallback_standby_unacknowledged", t0, evidence, lockout=True
            )

        neutral_ok = await self._wait_neutral(cancel_event=cancel_event)
        evidence["neutral_ok"] = neutral_ok
        if not neutral_ok:
            # Still attempt cleanup, but never claim verified.
            await self._restore_local_limits(cancel_event=cancel_event)
            await self._tracked_switch(self.remote_switch, False, cancel_event=cancel_event)
            return self._fallback_incomplete(
                command_id, reason, "fallback_neutral_timeout", t0, evidence, lockout=True
            )

        restores_verified = False
        if self._ha.number_register_ack_reliable:
            restore_failure = await self._restore_local_limits_verified(cancel_event=cancel_event)
            evidence["restore_failure"] = restore_failure
            if restore_failure is not None:
                await self._tracked_switch(self.remote_switch, False, cancel_event=cancel_event)
                return self._fallback_incomplete(
                    command_id,
                    reason,
                    f"fallback_restore_failed:{restore_failure}"[:60],
                    t0,
                    evidence,
                    lockout=True,
                )
            restores_verified = True
        else:
            await self._restore_local_limits(cancel_event=cancel_event)
            evidence["restore_failure"] = "number_register_ack_unreliable"

        off = await self._tracked_switch(self.remote_switch, False, cancel_event=cancel_event)
        evidence["remote_off_ack"] = off.status.value
        if off.status not in {AckStatus.ACKNOWLEDGED, AckStatus.IDEMPOTENT_NOOP}:
            return self._fallback_incomplete(
                command_id, reason, "fallback_remote_off_failed", t0, evidence, lockout=True
            )

        local_ok = await self._wait_local_after_fallback(cancel_event=cancel_event)
        evidence["local_behavior_ok"] = local_ok
        if not local_ok or not restores_verified:
            return self._fallback_incomplete(
                command_id,
                reason,
                (
                    "fallback_local_behavior_unverified"
                    if not local_ok
                    else "fallback_restores_unverified"
                ),
                t0,
                evidence,
                lockout=True,
            )

        self._last_direction = ControlDirection.FALLBACK
        return SigenergyControlResult(
            control=ControlResult(
                command_id=command_id,
                requested_state=ControllerState.FALLBACK,
                observed_state=ControllerState.DISARMED,
                entity_readback={
                    self.remote_switch: "off",
                    "fallback_verified": "true",
                    "standby_ack": str(evidence.get("standby_ack")),
                    "neutral_ok": str(evidence.get("neutral_ok")),
                    "local_behavior_ok": str(evidence.get("local_behavior_ok")),
                },
                physical_verified=True,
                retries=0,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                failure_reason=reason,
                lockout_reason=None,
            ),
            service_calls=list(self._service_calls),
            ack_results=list(self._ack_results),
        )

    async def _write_configured_limits(
        self, *, cancel_event: asyncio.Event | None
    ) -> SigenergyControlResult | None:
        """Write explicit local/global limits. Never use HA displayed 0.0 as originals."""
        if not self._ha.number_register_ack_reliable:
            # Record the blocker and refuse to continue into live command.
            self._ack_results.append(
                AckResult(
                    AckStatus.UNACKNOWLEDGED,
                    self._settings.battery_control_charge_limit_entity,
                    self._settings.battery_control_local_charge_limit_kw,
                    None,
                    "UNACKNOWLEDGED_LIMIT",
                )
            )
            return SigenergyControlResult(
                control=ControlResult(
                    command_id=str(uuid.uuid4()),
                    requested_state=ControllerState.PREFLIGHT,
                    observed_state=ControllerState.DISARMED,
                    entity_readback={},
                    physical_verified=False,
                    retries=0,
                    latency_ms=0.0,
                    failure_reason="UNACKNOWLEDGED_LIMIT",
                    lockout_reason=None,
                ),
                service_calls=list(self._service_calls),
                ack_results=list(self._ack_results),
            )
        pairs = [
            (
                self._settings.battery_control_charge_limit_entity,
                self._settings.battery_control_local_charge_limit_kw,
            ),
            (
                self._settings.battery_control_discharge_limit_entity,
                self._settings.battery_control_local_discharge_limit_kw,
            ),
            (
                self._settings.battery_control_charge_cutoff_entity,
                self._settings.battery_control_local_charge_cutoff_pct,
            ),
            (
                self._settings.battery_control_discharge_cutoff_entity,
                self._settings.battery_control_local_discharge_cutoff_pct,
            ),
        ]
        for entity_id, value in pairs:
            ack = await self._tracked_number(entity_id, value, cancel_event=cancel_event)
            if ack.status not in {
                AckStatus.ACKNOWLEDGED,
                AckStatus.IDEMPOTENT_NOOP,
                AckStatus.VALUE_COERCED,
            }:
                return SigenergyControlResult(
                    control=ControlResult(
                        command_id=str(uuid.uuid4()),
                        requested_state=ControllerState.PREFLIGHT,
                        observed_state=ControllerState.DISARMED,
                        entity_readback={},
                        physical_verified=False,
                        retries=0,
                        latency_ms=0.0,
                        failure_reason="UNACKNOWLEDGED_LIMIT",
                        lockout_reason=None,
                    ),
                    service_calls=list(self._service_calls),
                    ack_results=list(self._ack_results),
                )
        return None

    async def _restore_local_limits(self, *, cancel_event: asyncio.Event | None) -> None:
        # Best-effort cleanup; acknowledgement may be unavailable. Never marks verified.
        for entity_id, value in self._local_restore_pairs():
            try:
                await self._ha.call_service(
                    "number", "set_value", {"entity_id": entity_id, "value": value}
                )
                self._service_calls.append(
                    ("number", "set_value", {"entity_id": entity_id, "value": value})
                )
            except Exception as exc:  # noqa: BLE001 — fallback must continue
                logger.warning("local limit restore failed for %s: %s", entity_id, exc)

    async def _restore_local_limits_verified(
        self, *, cancel_event: asyncio.Event | None
    ) -> str | None:
        """Return entity_id on first failed restore ack, else None."""
        for entity_id, value in self._local_restore_pairs():
            ack = await self._tracked_number(entity_id, value, cancel_event=cancel_event)
            if ack.status not in {
                AckStatus.ACKNOWLEDGED,
                AckStatus.IDEMPOTENT_NOOP,
                AckStatus.VALUE_COERCED,
            }:
                return entity_id
        return None

    def _local_restore_pairs(self) -> tuple[tuple[str, float], ...]:
        s = self._settings
        return (
            (s.battery_control_charge_limit_entity, s.battery_control_local_charge_limit_kw),
            (s.battery_control_discharge_limit_entity, s.battery_control_local_discharge_limit_kw),
            (s.battery_control_charge_cutoff_entity, s.battery_control_local_charge_cutoff_pct),
            (
                s.battery_control_discharge_cutoff_entity,
                s.battery_control_local_discharge_cutoff_pct,
            ),
        )

    async def _enter_standby_neutral(self, *, cancel_event: asyncio.Event | None) -> AckResult:
        ack = await self._tracked_select(
            self.mode_select,
            self._settings.battery_control_mode_standby,
            cancel_event=cancel_event,
        )
        if ack.status in {AckStatus.ACKNOWLEDGED, AckStatus.IDEMPOTENT_NOOP}:
            await self._wait_neutral(cancel_event=cancel_event)
        return ack

    async def _ensure_idle_local(
        self, *, cancel_event: asyncio.Event | None
    ) -> SigenergyControlResult:
        return await self.fallback("idle", cancel_event=cancel_event)

    async def _wait_neutral(self, *, cancel_event: asyncio.Event | None) -> bool:
        band = self._settings.battery_control_standby_neutral_band_kw
        timeout = self._settings.battery_control_physical_verify_timeout_seconds
        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return False
            physical = await self._read_physical()
            if physical.battery_power_kw is not None and abs(physical.battery_power_kw) <= band:
                return True
            await self._sleep(0.2)
        return False

    async def _wait_local_after_fallback(self, *, cancel_event: asyncio.Event | None) -> bool:
        """Require Remote EMS off plus bounded battery activity after fallback."""
        band = self._settings.battery_control_standby_neutral_band_kw
        timeout = self._settings.battery_control_physical_verify_timeout_seconds
        deadline = self._monotonic() + timeout
        good = 0
        while self._monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return False
            remote = await self._ha.get_state(self.remote_switch)
            if remote is None or remote.state != "off":
                good = 0
                await self._sleep(0.2)
                continue
            physical = await self._read_physical()
            if physical.battery_power_kw is not None and abs(physical.battery_power_kw) <= band:
                good += 1
                if good >= _STABLE_GOOD_SAMPLES:
                    return True
            else:
                good = 0
            await self._sleep(0.2)
        return False

    async def _verify_charge_command(
        self,
        intent: BatteryControlIntent,
        *,
        min_kw: float,
        commanded_mode: str,
        commanded_charge_limit_kw: float,
        command_started_at: dt.datetime,
        cancel_event: asyncio.Event | None,
    ) -> tuple[bool, str | None]:
        timeout = self._settings.battery_control_physical_verify_timeout_seconds
        deadline = self._monotonic() + timeout
        good = 0
        last_reason: str | None = "no_physical_charge_response"
        while self._monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return False, "cancelled"
            physical = await self._read_physical()
            reason = self._reject_charge_sample(
                physical,
                intent,
                min_kw=min_kw,
                commanded_mode=commanded_mode,
                commanded_charge_limit_kw=commanded_charge_limit_kw,
                command_started_at=command_started_at,
            )
            if reason is None:
                good += 1
                if good >= _STABLE_GOOD_SAMPLES:
                    return True, None
            else:
                good = 0
                last_reason = reason
            await self._sleep(0.2)
        return False, last_reason

    def _reject_charge_sample(
        self,
        physical: PhysicalSnapshot,
        intent: BatteryControlIntent,
        *,
        min_kw: float,
        commanded_mode: str,
        commanded_charge_limit_kw: float,
        command_started_at: dt.datetime,
    ) -> str | None:
        s = self._settings
        deadband = s.battery_control_deadband_kw

        if physical.battery_power_kw is None:
            return "battery_power_missing"
        if physical.grid_import_kw is None or physical.grid_export_kw is None:
            return "grid_power_missing"
        if physical.soc_pct is None:
            return "soc_missing"
        if physical.ems_mode is None:
            return "ems_mode_missing"

        for label, updated in (
            ("battery_power", physical.battery_power_updated_at),
            ("grid_import", physical.grid_import_updated_at),
            ("grid_export", physical.grid_export_updated_at),
            ("soc", physical.soc_updated_at),
        ):
            if updated is None:
                return f"stale_timestamp_missing:{label}"
            if updated < command_started_at:
                return f"stale_pre_command:{label}"

        if physical.ems_mode != commanded_mode:
            return "wrong_ems_mode"
        if physical.battery_power_kw < min_kw:
            return "battery_charge_below_min"
        if physical.grid_export_kw > deadband:
            return "charge_with_export_contradiction"
        if intent.expected_grid_direction == "import":
            if physical.grid_import_kw < deadband and intent.grid_charge:
                return "expected_import_missing"
            if (
                intent.expected_grid_kw_min is not None
                and physical.grid_import_kw + 1e-9 < intent.expected_grid_kw_min
            ):
                return "grid_import_below_expected"
            if (
                intent.expected_grid_kw_max is not None
                and physical.grid_import_kw - 1e-9 > intent.expected_grid_kw_max
            ):
                return "grid_import_above_expected"
        if physical.grid_import_kw > s.battery_control_max_grid_import_kw + 1e-9:
            return "site_import_exceeded"
        if physical.grid_export_kw > s.battery_control_max_grid_export_kw + 1e-9:
            return "unexpected_export"
        if not intent.export and physical.grid_export_kw > deadband:
            return "unplanned_export"
        cutoff = intent.cutoff_soc_pct - s.battery_control_charge_cutoff_margin_pct
        if physical.soc_pct >= cutoff:
            return "soc_cutoff_breached"
        if (
            physical.charge_limit_kw is not None
            and abs(physical.charge_limit_kw - commanded_charge_limit_kw) > 0.05
        ):
            return "charge_limit_mismatch"
        return None

    async def _read_physical(self) -> PhysicalSnapshot:
        if self._physical is not None:
            return await self._physical.read_physical()
        entity_ids = [
            ENTITY_BATTERY_POWER,
            ENTITY_GRID_IMPORT_POWER,
            ENTITY_GRID_EXPORT_POWER,
            ENTITY_SOC,
            self.mode_select,
            self._settings.battery_control_charge_limit_entity,
            self._settings.battery_control_discharge_limit_entity,
            self._settings.battery_control_charge_cutoff_entity,
            self._settings.battery_control_discharge_cutoff_entity,
        ]
        states = await self._ha.get_states(entity_ids)
        batt = states.get(ENTITY_BATTERY_POWER)
        grid_in = states.get(ENTITY_GRID_IMPORT_POWER)
        grid_out = states.get(ENTITY_GRID_EXPORT_POWER)
        soc = states.get(ENTITY_SOC)
        mode = states.get(self.mode_select)
        charge_lim = states.get(self._settings.battery_control_charge_limit_entity)
        discharge_lim = states.get(self._settings.battery_control_discharge_limit_entity)
        charge_cut = states.get(self._settings.battery_control_charge_cutoff_entity)
        discharge_cut = states.get(self._settings.battery_control_discharge_cutoff_entity)
        now = dt.datetime.now(tz=dt.UTC)
        return PhysicalSnapshot(
            battery_power_kw=batt.as_float() if batt else None,
            grid_import_kw=grid_in.as_float() if grid_in else None,
            grid_export_kw=grid_out.as_float() if grid_out else None,
            soc_pct=soc.as_float() if soc else None,
            ems_mode=mode.state if mode else None,
            sampled_at=now,
            battery_power_updated_at=batt.last_updated if batt else None,
            grid_import_updated_at=grid_in.last_updated if grid_in else None,
            grid_export_updated_at=grid_out.last_updated if grid_out else None,
            soc_updated_at=soc.last_updated if soc else None,
            charge_limit_kw=charge_lim.as_float() if charge_lim else None,
            discharge_limit_kw=discharge_lim.as_float() if discharge_lim else None,
            charge_cutoff_pct=charge_cut.as_float() if charge_cut else None,
            discharge_cutoff_pct=discharge_cut.as_float() if discharge_cut else None,
        )

    async def _tracked_select(
        self, entity_id: str, option: str, *, cancel_event: asyncio.Event | None
    ) -> AckResult:
        self._service_calls.append(
            ("select", "select_option", {"entity_id": entity_id, "option": option})
        )
        ack = await self._ha.select_option(
            entity_id,
            option,
            timeout_s=self._settings.battery_control_command_timeout_seconds,
            poll_interval_s=self._settings.battery_control_command_poll_seconds,
            cancel_event=cancel_event,
        )
        self._ack_results.append(ack)
        return ack

    async def _tracked_switch(
        self, entity_id: str, on: bool, *, cancel_event: asyncio.Event | None
    ) -> AckResult:
        service = "turn_on" if on else "turn_off"
        self._service_calls.append(("switch", service, {"entity_id": entity_id}))
        ack = await self._ha.turn_switch(
            entity_id,
            on,
            timeout_s=self._settings.battery_control_command_timeout_seconds,
            poll_interval_s=self._settings.battery_control_command_poll_seconds,
            cancel_event=cancel_event,
        )
        self._ack_results.append(ack)
        return ack

    async def _tracked_number(
        self, entity_id: str, value: float, *, cancel_event: asyncio.Event | None
    ) -> AckResult:
        self._service_calls.append(
            ("number", "set_value", {"entity_id": entity_id, "value": value})
        )
        ack = await self._ha.set_number(
            entity_id,
            value,
            timeout_s=self._settings.battery_control_command_timeout_seconds,
            poll_interval_s=self._settings.battery_control_command_poll_seconds,
            cancel_event=cancel_event,
        )
        self._ack_results.append(ack)
        return ack

    def _fallback_incomplete(
        self,
        command_id: str,
        reason: str,
        incomplete_code: str,
        t0: float,
        evidence: dict[str, object],
        *,
        lockout: bool,
    ) -> SigenergyControlResult:
        self._consecutive_failures += 1
        lockout = lockout or self._consecutive_failures > self._settings.battery_control_retry_limit
        return SigenergyControlResult(
            control=ControlResult(
                command_id=command_id,
                requested_state=ControllerState.FALLBACK,
                observed_state=ControllerState.LOCKOUT if lockout else ControllerState.FALLBACK,
                entity_readback={
                    "fallback_verified": "false",
                    "incomplete": incomplete_code,
                    "standby_ack": str(evidence.get("standby_ack")),
                    "neutral_ok": str(evidence.get("neutral_ok")),
                    "remote_off_ack": str(evidence.get("remote_off_ack")),
                    "local_behavior_ok": str(evidence.get("local_behavior_ok")),
                },
                physical_verified=False,
                retries=self._consecutive_failures,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                failure_reason=incomplete_code,
                lockout_reason=incomplete_code if lockout else None,
            ),
            service_calls=list(self._service_calls),
            ack_results=list(self._ack_results),
            lockout=lockout,
        )

    async def _failure_fallback(
        self,
        command_id: str,
        reason: str,
        t0: float,
        cancel_event: asyncio.Event | None,
    ) -> SigenergyControlResult:
        self._consecutive_failures += 1
        result = await self.fallback(reason, command_id=command_id, cancel_event=cancel_event)
        if self._consecutive_failures > self._settings.battery_control_retry_limit:
            result.lockout = True
            result.control = ControlResult(
                command_id=result.control.command_id,
                requested_state=ControllerState.FALLBACK,
                observed_state=ControllerState.LOCKOUT,
                entity_readback=result.control.entity_readback,
                physical_verified=result.control.physical_verified,
                retries=self._consecutive_failures,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                failure_reason=reason,
                lockout_reason=reason,
            )
        return result

    def _fail(
        self,
        command_id: str,
        state: ControllerState,
        reason: str,
        t0: float,
        *,
        lockout: bool = False,
    ) -> SigenergyControlResult:
        return SigenergyControlResult(
            control=ControlResult(
                command_id=command_id,
                requested_state=state,
                observed_state=ControllerState.LOCKOUT if lockout else state,
                entity_readback={},
                physical_verified=False,
                retries=0,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                failure_reason=reason,
                lockout_reason=reason if lockout else None,
            ),
            service_calls=list(self._service_calls),
            ack_results=list(self._ack_results),
            lockout=lockout,
        )
