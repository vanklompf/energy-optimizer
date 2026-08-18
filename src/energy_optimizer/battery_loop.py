"""Battery-control loop: lease, authorize, actuate, fallback, heartbeat.

Extracted from Service so orchestration stays thin. Methods are mixed into Service.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from .config import Settings
from .ha_client import HaClient
from .mqtt_publish import MqttPublisher
from .safety import SafetyInputs, SafetyReport, evaluate
from .store import EvTelemetry, PlanStep, Price, Run, Store, Telemetry, utcnow
from .watchdog import watchdog_health_from_ha

logger = logging.getLogger(__name__)

# Attended commissioning only (ROADMAP Stage 1 checkpoints 1-3). The request lives in
# process memory and expires on its own so a forgotten arm cannot outlive the window or
# a restart.
_MANUAL_COMMAND_DEFAULT_DURATION_SECONDS = 300.0
_MANUAL_COMMAND_MAX_DURATION_SECONDS = 1800.0
_MANUAL_COMMAND_DIRECTIONS = ("CHARGE", "DISCHARGE", "EXPORT")
_MANUAL_COMMAND_REASON_CODES = {
    "CHARGE": ("manual_grid_charge_test",),
    "DISCHARGE": ("manual_discharge_test",),
    "EXPORT": ("manual_export_test",),
}

# Back-compat aliases for callers still using the charge-only names.
_MANUAL_CHARGE_DEFAULT_DURATION_SECONDS = _MANUAL_COMMAND_DEFAULT_DURATION_SECONDS
_MANUAL_CHARGE_MAX_DURATION_SECONDS = _MANUAL_COMMAND_MAX_DURATION_SECONDS


@dataclass(frozen=True, slots=True)
class ManualCommandRequest:
    """One attended battery command the control loop prefers over the plan.

    ``direction`` is one of ``CHARGE``, ``DISCHARGE`` (to house load), or ``EXPORT``
    (discharge to grid). It only selects *what* the loop commands; every gate, blocker,
    ramp limit, and physical verification still applies.
    """

    request_id: str
    direction: str
    target_kw: float
    requested_at: dt.datetime
    expires_at: dt.datetime


# Back-compat alias; older references may still import the charge-only name.
ManualChargeRequest = ManualCommandRequest


class BatteryMixin:
    """Battery actuation loop. Concrete Service supplies settings/store/MQTT."""

    settings: Settings
    store: Store
    controller_owner_id: str
    _battery_control_lock: asyncio.Lock
    _mqtt: MqttPublisher | None
    _manual_command: ManualCommandRequest | None

    def _latest_soc_with_age(self, now: dt.datetime) -> tuple[float | None, float | None]:
        raise NotImplementedError

    def _latest_ev_telemetry(self) -> EvTelemetry | None:
        raise NotImplementedError

    def _telemetry_stale(self, now: dt.datetime) -> tuple[bool, list[str]]:
        raise NotImplementedError

    def _future_prices(self, now: dt.datetime) -> list[Price]:
        raise NotImplementedError

    def _have_current_price(self, prices: list[Price], now: dt.datetime) -> bool:
        raise NotImplementedError

    def _known_price_hours(self, prices: list[Price], now: dt.datetime) -> float:
        raise NotImplementedError

    # --- attended manual command (charge / discharge / export) -------------
    def request_manual_command(
        self,
        *,
        direction: str,
        target_kw: float,
        duration_seconds: float | None = None,
        now: dt.datetime | None = None,
    ) -> ManualCommandRequest:
        """Arm one bounded battery command for an attended commissioning window.

        ``direction`` is ``CHARGE``, ``DISCHARGE`` (to house load), or ``EXPORT`` (to
        grid). The planner only chooses a direction when the price spread justifies it,
        so the Stage 1 checkpoints otherwise cannot be scheduled. This does not bypass
        any gate: mode, ``battery_control_enabled``, the per-direction gate, authorization
        blockers, the ramp limit, and physical verification all still apply.
        """
        s = self.settings
        now = now or utcnow()
        direction = direction.strip().upper()
        if direction not in _MANUAL_COMMAND_DIRECTIONS:
            raise ValueError(
                f"direction must be one of {_MANUAL_COMMAND_DIRECTIONS}"
            )
        duration = (
            _MANUAL_COMMAND_DEFAULT_DURATION_SECONDS
            if duration_seconds is None
            else float(duration_seconds)
        )

        # Refuse arming against a closed gate: otherwise every control cycle would fail
        # the intent and fall back. Mirror exactly the gates the intent builder enforces.
        if direction == "CHARGE":
            if not s.battery_control_grid_charge_enabled:
                raise ValueError("battery_control_grid_charge_enabled is false")
            max_kw = s.battery_control_max_charge_kw
            max_name = "battery_control_max_charge_kw"
        else:
            if not s.battery_control_authorize_discharge:
                raise ValueError("battery_control_authorize_discharge is false")
            if "DISCHARGE" not in s.battery_control_supported_directions:
                raise ValueError("DISCHARGE not in battery_control_supported_directions")
            if direction == "EXPORT" and not s.battery_export_enabled:
                raise ValueError("battery_export_enabled is false")
            max_kw = s.battery_control_max_discharge_kw
            max_name = "battery_control_max_discharge_kw"

        if target_kw < s.battery_control_deadband_kw:
            raise ValueError(
                f"target_kw must be at least the deadband {s.battery_control_deadband_kw} kW"
            )
        if target_kw > max_kw + 1e-9:
            raise ValueError(f"target_kw exceeds {max_name} ({max_kw} kW)")
        if duration <= 0 or duration > _MANUAL_COMMAND_MAX_DURATION_SECONDS:
            raise ValueError(
                f"duration_seconds must be within 0-{_MANUAL_COMMAND_MAX_DURATION_SECONDS:.0f}"
            )

        request = ManualCommandRequest(
            request_id=str(uuid.uuid4()),
            direction=direction,
            target_kw=float(target_kw),
            requested_at=now,
            expires_at=now + dt.timedelta(seconds=duration),
        )
        self._manual_command = request
        logger.warning(
            "manual %s command armed: %.2f kW until %s (request %s)",
            request.direction,
            request.target_kw,
            request.expires_at.isoformat(),
            request.request_id,
        )
        return request

    def request_manual_charge(
        self,
        *,
        target_kw: float,
        duration_seconds: float | None = None,
        now: dt.datetime | None = None,
    ) -> ManualCommandRequest:
        """Charge-only alias for :meth:`request_manual_command` (checkpoint 1)."""
        return self.request_manual_command(
            direction="CHARGE",
            target_kw=target_kw,
            duration_seconds=duration_seconds,
            now=now,
        )

    def clear_manual_command(self) -> None:
        """Drop any armed request; the next control cycle returns to the plan."""
        if self._manual_command is not None:
            logger.warning("manual command cleared: %s", self._manual_command.request_id)
        self._manual_command = None

    def clear_manual_charge(self) -> None:
        """Back-compat alias for :meth:`clear_manual_command`."""
        self.clear_manual_command()

    def active_manual_command(self, now: dt.datetime) -> ManualCommandRequest | None:
        request = self._manual_command
        if request is None:
            return None
        if now >= request.expires_at:
            logger.info("manual command expired: %s", request.request_id)
            self._manual_command = None
            return None
        return request

    def active_manual_charge(self, now: dt.datetime) -> ManualCommandRequest | None:
        """Back-compat alias for :meth:`active_manual_command`."""
        return self.active_manual_command(now)

    def manual_command_ramp_seconds(self, target_kw: float) -> float:
        """Cycles the 0.5 kW/step ramp needs to reach the target from a standstill."""
        s = self.settings
        if s.battery_control_max_power_step_kw <= 0:
            return 0.0
        steps = math.ceil(target_kw / s.battery_control_max_power_step_kw)
        return max(0, steps - 1) * s.battery_control_cadence_seconds

    def manual_charge_ramp_seconds(self, target_kw: float) -> float:
        """Back-compat alias for :meth:`manual_command_ramp_seconds`."""
        return self.manual_command_ramp_seconds(target_kw)

    def manual_command_status(self, now: dt.datetime | None = None) -> dict[str, object] | None:
        now = now or utcnow()
        request = self.active_manual_command(now)
        if request is None:
            return None
        return {
            "request_id": request.request_id,
            "direction": request.direction,
            "target_kw": request.target_kw,
            "requested_at": request.requested_at.isoformat(),
            "expires_at": request.expires_at.isoformat(),
            "seconds_remaining": max(0.0, (request.expires_at - now).total_seconds()),
            "ramp_estimate_seconds": self.manual_command_ramp_seconds(request.target_kw),
        }

    def manual_charge_status(self, now: dt.datetime | None = None) -> dict[str, object] | None:
        """Back-compat alias for :meth:`manual_command_status`."""
        return self.manual_command_status(now)

    # --- battery control ---------------------------------------------------
    def _manual_command_flows(
        self,
        manual: ManualCommandRequest,
        *,
        interval_start: dt.datetime,
        dt_hours: float,
    ):
        """Translate a manual request into the plan-flow snapshot for its direction.

        Routing energy through the same ``PlanFlowSnapshot`` the planner uses keeps every
        gate, cut-off, and expected-grid-bound check identical to plan-driven commands.
        DISCHARGE keeps both grid flows at zero so any measured export trips
        ``unplanned_export``; EXPORT declares the export flow so ``battery_export_enabled``
        is required and the export bounds are verified.
        """
        from .battery_control import PlanFlowSnapshot

        energy = manual.target_kw * dt_hours
        grid_to_battery = energy if manual.direction == "CHARGE" else 0.0
        battery_to_load = energy if manual.direction == "DISCHARGE" else 0.0
        battery_to_grid = energy if manual.direction == "EXPORT" else 0.0
        flows = PlanFlowSnapshot(
            interval_start=interval_start,
            dt_hours=dt_hours,
            pv_to_battery_kwh=0.0,
            grid_to_battery_kwh=grid_to_battery,
            battery_to_load_kwh=battery_to_load,
            battery_to_grid_kwh=battery_to_grid,
            battery_to_ev_kwh=0.0,
            grid_import_kwh=grid_to_battery,
            grid_export_kwh=battery_to_grid,
        )
        return flows, _MANUAL_COMMAND_REASON_CODES[manual.direction]

    def build_battery_control_intent(self, now: dt.datetime | None = None):
        """Select the plan interval containing now and translate it to a typed intent."""
        from .battery_control import PlanFlowSnapshot, intent_from_plan_flows
        from .safety import select_current_interval

        now = now or utcnow()
        s = self.settings
        with self.store.session() as session:
            run = session.execute(
                select(Run)
                .where(Run.status.in_(("ok", "low_confidence")))
                .order_by(Run.ts.desc())
                .limit(1)
            ).scalar_one_or_none()
            if run is None:
                raise ValueError("no successful plan available")
            steps = (
                session.execute(
                    select(PlanStep)
                    .where(PlanStep.run_id == run.run_id)
                    .order_by(PlanStep.interval_start)
                )
                .scalars()
                .all()
            )
        starts = [_aware(step.interval_start) for step in steps]
        selected = select_current_interval(
            starts, now=now, step_minutes=s.step_minutes, tz=s.tz
        )
        manual = self.active_manual_command(now)
        if selected is None:
            if manual is None:
                raise ValueError("no current plan interval")
            # The optimiser often starts at the next 15-minute slot, so there is a
            # gap with no current plan interval. A manual charge still has to run.
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(s.tz)
            local = now.astimezone(zone)
            minute = (local.minute // s.step_minutes) * s.step_minutes
            local_start = local.replace(minute=minute, second=0, microsecond=0)
            interval_start = local_start.astimezone(dt.UTC)
            interval_end = interval_start + dt.timedelta(minutes=s.step_minutes)
            dt_hours = s.step_minutes / 60.0
        else:
            interval_start, interval_end = selected
            step = next(st for st in steps if _aware(st.interval_start) == interval_start)
            dt_hours = step.dt_hours
        expiry = interval_end
        reason_codes: tuple[str, ...] = ()
        if manual is not None:
            # Reuse the plan-flow translation so the manual intent carries the same gate
            # check, cut-off, and expected grid bounds the verifier relies on.
            flows, reason_codes = self._manual_command_flows(
                manual, interval_start=interval_start, dt_hours=dt_hours
            )
            expiry = min(interval_end, manual.expires_at)
        else:
            flows = PlanFlowSnapshot(
                interval_start=interval_start,
                dt_hours=step.dt_hours,
                pv_to_battery_kwh=step.pv_to_battery_kwh,
                grid_to_battery_kwh=step.grid_to_battery_kwh,
                battery_to_load_kwh=step.battery_to_load_kwh,
                battery_to_grid_kwh=step.battery_to_grid_kwh,
                battery_to_ev_kwh=0.0,
                grid_import_kwh=step.grid_to_load_kwh + step.grid_to_battery_kwh,
                grid_export_kwh=step.pv_to_grid_kwh + step.battery_to_grid_kwh,
                pv_to_grid_kwh=step.pv_to_grid_kwh,
            )
        intent = intent_from_plan_flows(
            flows,
            settings=s,
            source_run_id=None if run.run_id is None else abs(hash(run.run_id)) % (10**9),
            now=now,
            expiry=expiry,
            reason_codes=reason_codes,
        )
        plan_age = max(0.0, (now - _aware(run.ts)).total_seconds())
        return intent, run.run_id, interval_start, plan_age

    def _previous_control_context(self, now: dt.datetime):
        """Recover the last direction and battery power for reversal and ramp limits.

        A fresh SigenergyController is built every cycle, so its in-process
        ``_last_direction`` always starts at IDLE and would skip the neutral dwell on
        charge/discharge reversal. Persisted controller state survives the cycle, and
        measured battery power is the physically meaningful ramp baseline. Missing or
        stale telemetry yields 0.0, which clamps the next command to a single step.
        """
        from .battery_control import direction_from_controller_state
        from .control_store import ensure_controller_state

        s = self.settings
        with self.store.session() as session:
            state = ensure_controller_state(session)
            previous_direction = direction_from_controller_state(state.state)
            tele = session.execute(
                select(Telemetry).order_by(Telemetry.ts.desc()).limit(1)
            ).scalar_one_or_none()
        previous_power_kw = 0.0
        if tele is not None:
            age = max(0.0, (now - _aware(tele.ts)).total_seconds())
            if age <= s.battery_control_max_telemetry_age_seconds:
                previous_power_kw = abs(
                    (tele.batt_charge_kw or 0.0) - (tele.batt_discharge_kw or 0.0)
                )
        return previous_direction, previous_power_kw

    async def control_battery(
        self, now: dt.datetime | None = None, *, force_fallback: bool = False
    ) -> dict[str, object]:
        """Lease, authorize, optionally actuate, audit, and publish battery control status."""
        async with self._battery_control_lock:
            return await self._control_battery_locked(now=now, force_fallback=force_fallback)

    async def _control_battery_locked(
        self, *, now: dt.datetime | None, force_fallback: bool
    ) -> dict[str, object]:
        from .battery_control import clamp_intent_power
        from .control_store import (
            ensure_controller_state,
            expire_lockout_if_due,
            finalize_action,
            is_locked_out,
            persist_pending_action,
            set_lockout,
            try_acquire_lease,
        )
        from .sigenergy_control import SigenergyController

        now = now or utcnow()
        s = self.settings
        command_id = str(uuid.uuid4())
        with self.store.session() as session:
            expire_lockout_if_due(session, now=now)

        if force_fallback:
            return await self.fallback_battery("force_fallback", command_id=command_id)

        with self.store.session() as session:
            locked_out = is_locked_out(session, now=now)
            state = ensure_controller_state(session)
            path_loss_recovery_needed = (
                locked_out and state.lockout_reason == "fallback_ha_unreachable"
            )
            if locked_out and not path_loss_recovery_needed:
                return {"result": "lockout", "command_id": command_id}
            if not locked_out and not try_acquire_lease(
                session,
                owner_id=self.controller_owner_id,
                ttl_seconds=s.battery_control_heartbeat_expiry_seconds,
                now=now,
            ):
                set_lockout(
                    session,
                    reason="lease_conflict",
                    duration_seconds=s.battery_control_lockout_duration_seconds,
                    now=now,
                )
                return {"result": "lease_conflict", "command_id": command_id}

        if path_loss_recovery_needed:
            return await self.reconcile_path_loss_recovery()

        shadow = not s.battery_actuation_live

        try:
            intent, run_id, interval_start, plan_age = self.build_battery_control_intent(now)
        except ValueError as exc:
            if shadow:
                with self.store.session() as session:
                    persist_pending_action(
                        session,
                        command_id=command_id,
                        source_run_id=None,
                        interval_start=None,
                        intent={"direction": "IDLE", "shadow": True, "error": str(exc)},
                        authorization_allowed=False,
                        blockers=[f"stale_plan:{exc}"],
                        requested_state="IDLE",
                    )
                    finalize_action(
                        session,
                        command_id,
                        observed_state="DISARMED",
                        physical={"shadow": True, "ha_writes": 0},
                        result="shadow",
                        error_code=f"stale_plan:{exc}"[:64],
                    )
                return {
                    "result": "shadow",
                    "command_id": command_id,
                    "blockers": [f"stale_plan:{exc}"],
                }
            reason = f"stale_plan:{exc}"[:60]
            return await self.fallback_battery(reason, command_id=command_id)

        # Ramp-limit before persisting so the audit records what is actually commanded,
        # and so dry-run previews the same power the live path would write.
        previous_direction, previous_power_kw = self._previous_control_context(now)
        intent = clamp_intent_power(intent, settings=s, previous_power_kw=previous_power_kw)

        safety, evidence = await self._evaluate_battery_authorization(
            intent,
            now=now,
            interval_start=interval_start,
            plan_age=plan_age,
            lease_held=True,
            shadow=shadow,
        )
        intent_payload = {
            "direction": intent.direction.value,
            "requested_power_kw": intent.requested_power_kw,
            "cutoff_soc_pct": intent.cutoff_soc_pct,
            "expiry": intent.expiry.isoformat(),
            "grid_charge": intent.grid_charge,
            "export": intent.export,
            "expected_grid_direction": intent.expected_grid_direction,
            "expected_grid_kw_min": intent.expected_grid_kw_min,
            "expected_grid_kw_max": intent.expected_grid_kw_max,
            "expected_financial_value_pln": intent.expected_financial_value_pln,
            "reason_codes": list(intent.reason_codes),
            "shadow": shadow,
            "evidence": evidence,
        }
        with self.store.session() as session:
            persist_pending_action(
                session,
                command_id=command_id,
                source_run_id=run_id,
                interval_start=interval_start,
                intent=intent_payload,
                authorization_allowed=safety.control_authorized,
                blockers=safety.control_blockers,
                requested_state=intent.direction.value,
            )

        if shadow:
            # Hard no-write boundary: never instantiate SigenergyController / HaClient here.
            with self.store.session() as session:
                finalize_action(
                    session,
                    command_id,
                    observed_state="DISARMED",
                    physical={
                        "shadow": True,
                        "ha_writes": 0,
                        "would_authorize": evidence.get("would_authorize", False),
                    },
                    result="shadow",
                    error_code=(
                        None
                        if evidence.get("would_authorize")
                        else (",".join(safety.control_blockers) or "not_authorized")[:64]
                    ),
                )
            return {
                "result": "shadow",
                "command_id": command_id,
                "direction": intent.direction.value,
                "requested_power_kw": intent.requested_power_kw,
                "authorized": safety.control_authorized,
                "would_authorize": evidence.get("would_authorize", False),
                "blockers": safety.control_blockers,
                "source_run_id": run_id,
                "interval_start": interval_start.isoformat(),
            }

        if not safety.control_authorized:
            with self.store.session() as session:
                finalize_action(
                    session,
                    command_id,
                    observed_state="DISARMED",
                    physical=None,
                    result="blocked",
                    error_code=(",".join(safety.control_blockers) or "not_authorized")[:64],
                )
            return {
                "result": "blocked",
                "command_id": command_id,
                "blockers": safety.control_blockers,
            }

        async with HaClient(
            s.ha_url,
            s.ha_token,
            verify_ssl=s.ha_verify_ssl,
        ) as ha:
            controller = SigenergyController(ha, s)
            outcome = await controller.apply_intent(
                intent, previous_direction=previous_direction
            )

        with self.store.session() as session:
            finalize_action(
                session,
                command_id,
                observed_state=(
                    outcome.control.observed_state.value
                    if outcome.control.observed_state
                    else None
                ),
                physical={"verified": outcome.control.physical_verified},
                result="ok" if outcome.control.failure_reason is None else "failed",
                error_code=outcome.control.failure_reason,
                latency_ms=outcome.control.latency_ms,
            )
            state = ensure_controller_state(session)
            if outcome.lockout:
                set_lockout(
                    session,
                    reason=outcome.control.lockout_reason or "control_failure",
                    duration_seconds=s.battery_control_lockout_duration_seconds,
                    now=now,
                )
            elif outcome.control.failure_reason is None:
                state.state = (
                    outcome.control.observed_state.value
                    if outcome.control.observed_state
                    else state.state
                )
                state.last_successful_command_id = command_id
                state.consecutive_failures = 0
                state.updated_at = now
        return {
            "result": "ok" if outcome.control.failure_reason is None else "failed",
            "command_id": command_id,
            "failure_reason": outcome.control.failure_reason,
        }

    async def fallback_battery(
        self, reason: str, *, command_id: str | None = None
    ) -> dict[str, object]:
        """Plan-independent fallback to local EMS. Safe when optimizer is down."""
        from .control_store import (
            ensure_controller_state,
            finalize_action,
            persist_pending_action,
            set_lockout,
        )
        from .sigenergy_control import SigenergyController

        s = self.settings
        command_id = command_id or str(uuid.uuid4())
        with self.store.session() as session:
            persist_pending_action(
                session,
                command_id=command_id,
                source_run_id=None,
                interval_start=None,
                intent={"direction": "FALLBACK", "reason": reason},
                authorization_allowed=True,
                blockers=[],
                requested_state="FALLBACK",
            )

        if not s.battery_actuation_live or not s.ha_token:
            with self.store.session() as session:
                finalize_action(
                    session,
                    command_id,
                    observed_state="DISARMED",
                    physical=None,
                    result="fallback_recorded",
                    error_code=reason,
                )
                state = ensure_controller_state(session)
                state.last_fallback_at = utcnow()
                state.last_fallback_verified = False
                state.state = "DISARMED"
            return {
                "result": "fallback_recorded",
                "command_id": command_id,
                "reason": reason,
                "verified": False,
            }

        try:
            async with HaClient(
                s.ha_url,
                s.ha_token,
                verify_ssl=s.ha_verify_ssl,
            ) as ha:
                outcome = await SigenergyController(ha, s).fallback(reason, command_id=command_id)
        except Exception as exc:  # Preserve an auditable lockout if fallback cannot reach HA.
            logger.error("battery fallback could not reach Home Assistant: %s", exc)
            now = utcnow()
            with self.store.session() as session:
                finalize_action(
                    session,
                    command_id,
                    observed_state="LOCKOUT",
                    physical={"verified": False, "ha_reachable": False},
                    result="fallback_unreachable",
                    error_code=reason,
                )
                state = ensure_controller_state(session)
                state.last_fallback_at = now
                state.last_fallback_verified = False
                set_lockout(
                    session,
                    reason="fallback_ha_unreachable",
                    duration_seconds=s.battery_control_lockout_duration_seconds,
                    now=now,
                )
            return {
                "result": "fallback_unreachable",
                "command_id": command_id,
                "reason": reason,
                "verified": False,
            }

        verified = bool(outcome.control.physical_verified)
        requires_lockout = bool(getattr(outcome, "lockout", False)) or not verified
        with self.store.session() as session:
            finalize_action(
                session,
                command_id,
                observed_state=(
                    "LOCKOUT"
                    if requires_lockout
                    else (
                        outcome.control.observed_state.value
                        if outcome.control.observed_state
                        else "FALLBACK"
                    )
                ),
                physical={"verified": verified},
                result="fallback",
                error_code=reason,
                latency_ms=outcome.control.latency_ms,
            )
            state = ensure_controller_state(session)
            state.last_fallback_at = utcnow()
            state.last_fallback_verified = verified
            if requires_lockout:
                set_lockout(
                    session,
                    reason="fallback_unverified",
                    duration_seconds=s.battery_control_lockout_duration_seconds,
                )
            else:
                state.state = (
                    outcome.control.observed_state.value
                    if outcome.control.observed_state
                    else "FALLBACK"
                )
        return {
            "result": "fallback",
            "command_id": command_id,
            "reason": reason,
            "verified": bool(outcome.control.physical_verified),
        }

    async def reconcile_path_loss_recovery(self) -> dict[str, object]:
        """Make one OFF-only restore attempt after an HA-path-loss lockout.

        Economic control stays paused until the backoff expires (or an operator
        clears lockout). This never turns Remote EMS on.
        """
        from .control_store import claim_path_loss_recovery, ensure_controller_state

        s = self.settings
        if not s.battery_actuation_live:
            return {"result": "reconnect_reconcile_disarmed"}

        with self.store.session() as session:
            state = ensure_controller_state(session)
            if state.lockout_reason != "fallback_ha_unreachable":
                return {"result": "reconnect_reconcile_not_needed"}
            if not claim_path_loss_recovery(session):
                return {"result": "reconnect_recovery_already_claimed"}

        result = await self.fallback_battery("reconnect_after_path_loss")
        verified = bool(result.get("verified"))
        with self.store.session() as session:
            state = ensure_controller_state(session)
            state.state = "LOCKOUT"
            state.lockout_reason = (
                "path_recovered_restore_verified"
                if result.get("result") == "fallback" and verified
                else "path_loss_recovery_attempted_unverified"
            )
            state.updated_at = utcnow()
            if result.get("result") == "fallback" and verified:
                return {"result": "reconnect_restore_verified", "fallback": result}
        return {"result": "reconnect_restore_unverified", "fallback": result}

    async def reconcile_battery_on_startup(self) -> dict[str, object]:
        """Never trust persisted desired state; fall back if Remote EMS is unexpectedly on."""
        from .control_store import ensure_controller_state, list_pending_actions

        s = self.settings
        with self.store.session() as session:
            ensure_controller_state(session)
            for pending in list_pending_actions(session):
                pending.result = "abandoned_on_restart"
                pending.error_code = "restart_recovery"
                pending.updated_at = utcnow()

        if not s.battery_actuation_live or not s.ha_token:
            return {"result": "disarmed_startup"}

        try:
            async with HaClient(s.ha_url, s.ha_token, verify_ssl=s.ha_verify_ssl) as ha:
                remote = await ha.get_state(s.battery_control_remote_ems_switch_entity)
        except Exception as exc:  # HA state unknown is an unsafe restart condition.
            logger.error("startup Remote EMS state could not be read: %s", exc)
            return await self.fallback_battery("startup_remote_ems_unknown")
        if remote is not None and remote.state == "on":
            return await self.fallback_battery("startup_remote_ems_on")
        return {"result": "startup_ok"}

    async def shutdown_battery_control(self) -> dict[str, object]:
        from .control_store import release_lease

        result = await self.fallback_battery("shutdown")
        with self.store.session() as session:
            release_lease(session, owner_id=self.controller_owner_id)
        return result

    async def publish_battery_heartbeat(self) -> None:
        """Lightweight heartbeat independent of optimisation."""
        from .control_store import ensure_controller_state, lease_held_by
        from .mqtt_publish import BatteryControlMqttState
        from .store import ControlAction

        now = utcnow()
        s = self.settings
        with self.store.session() as session:
            state = ensure_controller_state(session)
            state.last_heartbeat_at = now
            state.updated_at = now
            lease_held = lease_held_by(
                session, owner_id=self.controller_owner_id, now=now
            )
            last = session.execute(
                select(ControlAction).order_by(ControlAction.created_at.desc()).limit(1)
            ).scalar_one_or_none()
            blockers = ""
            last_result = "none"
            if last is not None:
                last_result = last.result
                blockers = last.error_code or ""
            armed = s.battery_actuation_live
            effective = "DRY_RUN"
            if state.lockout_until is not None and _aware(state.lockout_until) > now:
                effective = "LOCKOUT"
            elif armed:
                effective = state.state
            payload = {
                "ts": now.isoformat(),
                "state": state.state,
                "owner_id": self.controller_owner_id,
                "expires_at": (
                    now + dt.timedelta(seconds=s.battery_control_heartbeat_expiry_seconds)
                ).isoformat(),
                "lockout": bool(state.lockout_until and _aware(state.lockout_until) > now),
            }
            mqtt_state = BatteryControlMqttState(
                battery_control_state=state.state,
                battery_control_effective=effective,
                battery_control_last_result=last_result,
                battery_control_blockers=blockers,
                battery_control_armed=armed,
                battery_control_lease_held=lease_held,
                battery_control_watchdog_healthy=False,
            )
        if self._mqtt is not None:
            try:
                self._mqtt.publish_battery_heartbeat(payload)
                self._mqtt.publish_battery_control_state(mqtt_state)
            except Exception as exc:  # pragma: no cover
                logger.warning("battery heartbeat publish failed: %s", exc)


    async def _watchdog_health(self, now: dt.datetime) -> tuple[bool, str]:
        """Read independent HA readiness and acknowledgement, failing closed on any gap."""
        s = self.settings
        if not (
            s.ha_token
            and s.battery_control_watchdog_health_entity
            and s.battery_control_watchdog_ack_entity
        ):
            return False, "watchdog_mapping_missing"
        try:
            async with HaClient(s.ha_url, s.ha_token, verify_ssl=s.ha_verify_ssl) as ha:
                states = await ha.get_states(
                    [
                        s.battery_control_watchdog_health_entity,
                        s.battery_control_watchdog_ack_entity,
                    ]
                )
        except Exception as exc:
            logger.warning("watchdog health check unavailable: %s", exc)
            return False, "watchdog_health_unavailable"
        return watchdog_health_from_ha(
            states.get(s.battery_control_watchdog_health_entity),
            states.get(s.battery_control_watchdog_ack_entity),
            now=now,
            timezone=s.tz,
            expiry_seconds=s.battery_control_heartbeat_expiry_seconds,
        )

    async def _evaluate_battery_authorization(
        self,
        intent,
        *,
        now: dt.datetime,
        interval_start: dt.datetime,
        plan_age: float,
        lease_held: bool,
        shadow: bool,
    ) -> tuple[SafetyReport, dict[str, object]]:
        """Build live-control SafetyInputs from real store evidence (fail-closed)."""
        from .battery_control import ControlDirection
        from .control_store import ensure_controller_state
        from .watchdog import HeartbeatSample, heartbeat_is_healthy

        s = self.settings
        economic = intent.direction in {ControlDirection.CHARGE, ControlDirection.DISCHARGE}
        soc_pct, soc_age = self._latest_soc_with_age(now)
        telemetry_stale, stale_reasons = self._telemetry_stale(now)

        with self.store.session() as session:
            tele = session.execute(
                select(Telemetry).order_by(Telemetry.ts.desc()).limit(1)
            ).scalar_one_or_none()
            price_floor = now.replace(minute=0, second=0, microsecond=0)
            price = session.execute(
                select(Price).where(Price.interval_start == price_floor)
            ).scalar_one_or_none()
            state = ensure_controller_state(session)
            heartbeat_at = state.last_heartbeat_at
            consecutive_failures = int(state.consecutive_failures or 0)

        telemetry_ages: dict[str, float | None] = {
            "soc": soc_age,
            "battery_power": None,
            "grid_import": None,
            "grid_export": None,
        }
        corroborating = False
        if tele is not None:
            age = max(0.0, (now - _aware(tele.ts)).total_seconds())
            telemetry_ages["battery_power"] = age
            telemetry_ages["grid_import"] = age
            telemetry_ages["grid_export"] = age
            corroborating = (
                age <= s.battery_control_max_telemetry_age_seconds
                and (
                    tele.batt_charge_kw is not None
                    or tele.batt_discharge_kw is not None
                    or tele.grid_import_kw is not None
                    or tele.grid_export_kw is not None
                )
            )

        buy = price.buy_gross if price is not None else None
        sell = price.sell_gross if price is not None else None
        price_is_real = bool(
            price is not None and price.source == "api" and buy is not None and sell is not None
        )
        price_age = (
            max(0.0, (now - _aware(price.fetched_at)).total_seconds())
            if price is not None
            else None
        )

        heartbeat = (
            HeartbeatSample(ts=_aware(heartbeat_at), retained=False)
            if heartbeat_at is not None
            else None
        )
        # A local publish timestamp is insufficient: HA must also report that its
        # independent fallback is ready and has ingested a current heartbeat.
        watchdog_healthy = False
        watchdog_reason = "watchdog_not_evaluated_dry_run"
        if s.mode == "control" and s.battery_control_enabled:
            ha_watchdog_healthy, watchdog_reason = await self._watchdog_health(now)
            watchdog_healthy = heartbeat_is_healthy(
                heartbeat,
                now=now,
                expiry_seconds=s.battery_control_heartbeat_expiry_seconds,
            ) and ha_watchdog_healthy
            if not watchdog_healthy and watchdog_reason == "ok":
                watchdog_reason = "local_heartbeat_stale"

        soc_at_boundary = False
        if soc_pct is not None:
            soc_at_boundary = (
                abs(soc_pct - s.battery_control_min_soc_pct) < 0.5
                or abs(soc_pct - s.battery_control_max_soc_pct) < 0.5
            )

        ev_live = self._latest_ev_telemetry()
        ev_fresh = bool(
            ev_live is not None
            and (now - _aware(ev_live.ts)).total_seconds()
            <= s.battery_control_max_telemetry_age_seconds
        )

        prices = self._future_prices(now)
        have_current_price = self._have_current_price(prices, now)
        known_hours = self._known_price_hours(prices, now)

        inputs = SafetyInputs(
            telemetry_stale=telemetry_stale,
            telemetry_stale_reasons=stale_reasons,
            have_current_price=have_current_price,
            have_pv_forecast=True,  # live control does not re-fetch forecasts here
            have_load_forecast=True,
            known_price_hours=known_hours,
            horizon_hours=known_hours,  # control plans only over published prices
            min_price_hours=s.optimise_min_price_hours,
            mode_is_control=s.mode == "control",
            battery_control_enabled=s.battery_control_enabled,
            lease_held=lease_held,
            watchdog_healthy=watchdog_healthy,
            economic_action=economic,
            plan_status_ok=True,
            plan_age_seconds=plan_age,
            max_plan_age_seconds=s.battery_control_max_plan_age_seconds,
            max_telemetry_age_seconds=s.battery_control_max_telemetry_age_seconds,
            current_interval_start=interval_start,
            current_interval_end=interval_start + dt.timedelta(minutes=s.step_minutes),
            now=now,
            current_buy_price=buy,
            current_sell_price=sell,
            current_price_is_real=price_is_real,
            current_price_age_seconds=price_age,
            telemetry_ages_seconds=telemetry_ages,
            soc_pct=soc_pct,
            soc_update_age_seconds=soc_age,
            soc_at_boundary=soc_at_boundary,
            corroborating_power_fresh=corroborating,
            recent_command_failures=consecutive_failures,
            ev_goal_active=False,
            ev_telemetry_fresh=ev_fresh,
        )
        safety = evaluate(inputs)
        gate_blockers = {"mode_not_control", "battery_control_disabled"}
        remaining = [b for b in safety.control_blockers if b not in gate_blockers]
        evidence: dict[str, object] = {
            "shadow": shadow,
            "plan_age_seconds": plan_age,
            "price_is_real": price_is_real,
            "price_age_seconds": price_age,
            "price_fetched_at": price.fetched_at.isoformat() if price is not None else None,
            "telemetry_ts": tele.ts.isoformat() if tele is not None else None,
            "telemetry_ages_seconds": telemetry_ages,
            "soc_pct": soc_pct,
            "soc_age_seconds": soc_age,
            "corroborating_power_fresh": corroborating,
            "watchdog_healthy": watchdog_healthy,
            "watchdog_reason": watchdog_reason,
            "heartbeat_at": heartbeat_at.isoformat() if heartbeat_at is not None else None,
            "lease_held": lease_held,
            "would_authorize": not remaining,
            "non_gate_blockers": remaining,
        }
        return safety, evidence


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
