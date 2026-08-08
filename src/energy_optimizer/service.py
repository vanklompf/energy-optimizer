"""Application service: orchestrates collection, optimisation and publishing.

Holds the long-lived dependencies (settings, store, MQTT publisher) and exposes the unit
jobs the scheduler calls. Each optimise run writes an auditable ``runs`` + ``plan_steps``
record including an immutable, hashed ``solver_input`` snapshot.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import math
import uuid
from dataclasses import asdict
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from .config import Settings
from .ev import (
    EV_FULL_TARGET_SOC_PCT,
    EvRequirements,
    build_ev_requirements,
    same_day_forecast_surplus_kwh,
)
from .ev_control import EvControlDecision, EvLiveState, decide_ev_control
from .explain import classify_next_action
from .forecast.load import LoadForecaster, LoadSample
from .forecast.pv import PvForecaster
from .ha_client import (
    ENTITY_BATTERY_POWER,
    ENTITY_CONSUMED_POWER,
    ENTITY_GRID_EXPORT_POWER,
    ENTITY_GRID_IMPORT_POWER,
    ENTITY_PV_POWER,
    ENTITY_SOC,
    HaClient,
    HaState,
    _split_battery_power,
)
from .mqtt_publish import MqttConfig, MqttPublisher, RecommendationState
from .optimiser import IntervalInput, OptimiserParams, optimise
from .pstryk_client import PstrykClient
from .safety import CONTROL_ENABLED, SafetyInputs, SafetyReport, Status, evaluate
from .store import (
    EvControlStatus,
    EvPlanStep,
    EvTelemetry,
    Forecast,
    PlanStep,
    Price,
    PstrykMeterInterval,
    Run,
    Store,
    Telemetry,
    utcnow,
)
from .telemetry_energy import complete_hourly_energy

LOAD_LOOKBACK_DAYS = 28

logger = logging.getLogger(__name__)

SOLVER_INPUT_SCHEMA = "2"


class Service:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._mqtt: MqttPublisher | None = None
        self.ev_charge_to_100_active = False
        self.controller_owner_id = str(uuid.uuid4())
        self._battery_control_lock = asyncio.Lock()

    # --- lifecycle ---------------------------------------------------------
    def start_mqtt(self) -> None:
        s = self.settings
        if not s.mqtt_enabled or not s.mqtt_host:
            logger.info("MQTT disabled or host unset; skipping MQTT startup")
            return
        cfg = MqttConfig(
            host=s.mqtt_host,
            port=s.mqtt_port,
            username=s.mqtt_username,
            password=s.mqtt_password,
            tls=s.mqtt_tls,
            discovery_prefix=s.mqtt_discovery_prefix,
            node_id=s.mqtt_node_id,
            client_id=s.mqtt_client_id,
        )
        try:
            pub = MqttPublisher(cfg)
            pub.connect()
            pub.publish_discovery()
            self._mqtt = pub
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("MQTT startup failed: %s", exc)
            self._mqtt = None

    def stop_mqtt(self) -> None:
        if self._mqtt is not None:
            try:
                self._mqtt.disconnect()
            finally:
                self._mqtt = None

    # --- jobs --------------------------------------------------------------
    async def collect_telemetry(self) -> None:
        s = self.settings
        if not s.ha_token:
            logger.debug("No HA token configured; skipping telemetry collection")
            return
        async with HaClient(s.ha_url, s.ha_token, verify_ssl=s.ha_verify_ssl) as ha:
            snap = await ha.snapshot()
        with self.store.session() as session:
            session.merge(
                Telemetry(
                    ts=snap.ts,
                    soc_pct=snap.soc_pct,
                    batt_charge_kw=snap.batt_charge_kw,
                    batt_discharge_kw=snap.batt_discharge_kw,
                    pv_kw=snap.pv_kw,
                    load_kw=snap.load_kw,
                    grid_import_kw=snap.grid_import_kw,
                    grid_export_kw=snap.grid_export_kw,
                    ems_mode=snap.ems_mode,
                    stale=snap.stale,
                )
            )
        logger.info("Collected telemetry (stale=%s)", snap.stale)

    async def collect_ev_telemetry(self) -> None:
        """Collect EV SoC/plug state and charger relay protection telemetry."""
        s = self.settings
        if not s.ha_token:
            return
        entity_ids = [
            s.ev_soc_entity,
            s.ev_charging_status_entity,
            s.ev_charging_active_entity,
            s.ev_charge_to_100_entity,
            s.ev_switch_entity,
            s.ev_power_entity,
            *s.ev_fault_entities,
        ]
        async with HaClient(s.ha_url, s.ha_token, verify_ssl=s.ha_verify_ssl) as ha:
            states = await ha.get_states(entity_ids)
        soc = states.get(s.ev_soc_entity)
        status = states.get(s.ev_charging_status_entity)
        active = states.get(s.ev_charging_active_entity)
        charge_to_100 = states.get(s.ev_charge_to_100_entity)
        switch = states.get(s.ev_switch_entity)
        power = states.get(s.ev_power_entity)
        fault_states = [states.get(entity_id) for entity_id in s.ev_fault_entities]
        fault, fault_stale = _ev_fault_status(fault_states)
        self.ev_charge_to_100_active = _state_bool(charge_to_100) is True
        switch_on = _state_bool(switch)
        soc_pct = soc.as_float() if soc else None
        charging_status = (
            status.state if status and status.state not in {"unknown", "unavailable"} else None
        )
        power_w = power.as_float() if power else None
        row = EvTelemetry(
            ts=utcnow(),
            soc_pct=soc_pct,
            charging_status=charging_status,
            charging_active=_state_bool(active),
            switch_on=switch_on,
            switch_changed=switch.last_changed if switch else None,
            power_kw=power_w / 1000.0 if power_w is not None else None,
            fault=fault,
            stale=fault_stale or soc_pct is None or charging_status is None or switch_on is None,
        )
        with self.store.session() as session:
            session.add(row)
        logger.info(
            "Collected EV telemetry (soc=%s, status=%s, switch=%s, fault=%s)",
            row.soc_pct,
            row.charging_status,
            row.switch_on,
            row.fault,
        )

    async def control_ev_charging(
        self, now: dt.datetime | None = None, *, force_off: bool = False
    ) -> None:
        """Apply the current EV plan, or force an OFF-only recovery decision."""
        s = self.settings
        if not s.ev_control_enabled:
            return
        now = now or utcnow()
        ev = self._latest_ev_telemetry()
        planned_on: bool | None = None
        previous_control: EvControlStatus | None = None
        with self.store.session() as session:
            previous_control = session.get(EvControlStatus, "current")
            run = session.execute(select(Run).order_by(Run.ts.desc()).limit(1)).scalar_one_or_none()
            if run is not None and (now - _aware(run.ts)).total_seconds() <= 30 * 60:
                step = session.execute(
                    select(EvPlanStep)
                    .where(EvPlanStep.run_id == run.run_id)
                    .where(EvPlanStep.interval_start <= now)
                    .order_by(EvPlanStep.interval_start.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if step is not None:
                    step_start = _aware(step.interval_start)
                    if now < step_start + dt.timedelta(minutes=s.step_minutes):
                        planned_on = bool(step.planned_on)

        if force_off:
            planned_on = None
        live = _ev_live_state(ev, now)
        decision = (
            EvControlDecision(False, "turn_off", "upstream pipeline failure; forcing OFF")
            if force_off
            else decide_ev_control(
                s,
                live,
                planned_on=planned_on,
                now=now,
                force_charge=self.ev_charge_to_100_active,
            )
        )
        decision = _relay_failure_backoff_decision(
            decision,
            previous_control,
            now,
            s.ev_relay_failure_backoff_minutes,
        )
        if decision.action != "none":
            if not s.ha_token:
                decision = EvControlDecision(
                    False,
                    "turn_off",
                    "CRITICAL: Home Assistant credential unavailable; OFF could not be attempted",
                )
                logger.error("EV charger actuation alarm: %s", decision.reason)
            else:
                try:
                    async with HaClient(s.ha_url, s.ha_token, verify_ssl=s.ha_verify_ssl) as ha:
                        decision = await _apply_ev_relay_decision(
                            ha,
                            s.ev_switch_entity,
                            decision,
                            settle_seconds=s.ev_relay_settle_seconds,
                            verify_interval_seconds=s.ev_relay_verify_interval_seconds,
                            verify_timeout_seconds=s.ev_relay_verify_timeout_seconds,
                        )
                except Exception as exc:
                    logger.exception("EV Home Assistant control channel unavailable")
                    decision = EvControlDecision(
                        False,
                        "turn_off",
                        "CRITICAL: Home Assistant control channel unavailable "
                        f"({type(exc).__name__}); OFF could not be confirmed",
                    )
        with self.store.session() as session:
            status_ts = (
                previous_control.ts
                if previous_control is not None
                and decision.action == "none"
                and "relay retry backoff" in decision.reason
                else now
            )
            session.merge(
                EvControlStatus(
                    key="current",
                    ts=status_ts,
                    desired_on=decision.desired_on,
                    planned_on=planned_on,
                    action=decision.action,
                    reason=decision.reason,
                )
            )
        logger.info("EV control action=%s reason=%s", decision.action, decision.reason)

    # --- battery control ---------------------------------------------------
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
        if selected is None:
            raise ValueError("no current plan interval")
        interval_start, interval_end = selected
        step = next(st for st in steps if _aware(st.interval_start) == interval_start)
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
            expiry=interval_end,
        )
        plan_age = max(0.0, (now - _aware(run.ts)).total_seconds())
        return intent, run.run_id, interval_start, plan_age

    async def control_battery(
        self, now: dt.datetime | None = None, *, force_fallback: bool = False
    ) -> dict[str, object]:
        """Lease, authorize, optionally actuate, audit, and publish battery control status."""
        async with self._battery_control_lock:
            return await self._control_battery_locked(now=now, force_fallback=force_fallback)

    async def _control_battery_locked(
        self, *, now: dt.datetime | None, force_fallback: bool
    ) -> dict[str, object]:
        from .battery_control import ControlDirection
        from .control_store import (
            ensure_controller_state,
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

        if force_fallback:
            return await self.fallback_battery("force_fallback", command_id=command_id)

        with self.store.session() as session:
            if is_locked_out(session, now=now):
                return {"result": "lockout", "command_id": command_id}
            if not try_acquire_lease(
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
            ensure_controller_state(session)

        if s.mode != "control" or not s.battery_control_enabled:
            with self.store.session() as session:
                persist_pending_action(
                    session,
                    command_id=command_id,
                    source_run_id=None,
                    interval_start=None,
                    intent={"direction": "IDLE", "dry_run": True},
                    authorization_allowed=False,
                    blockers=["dry_run_or_disarmed"],
                    requested_state="DISARMED",
                )
                finalize_action(
                    session,
                    command_id,
                    observed_state="DISARMED",
                    physical=None,
                    result="dry_run_skipped",
                    error_code="dry_run_or_disarmed",
                )
            return {"result": "dry_run_skipped", "command_id": command_id}

        try:
            intent, run_id, interval_start, plan_age = self.build_battery_control_intent(now)
        except ValueError as exc:
            reason = f"stale_plan:{exc}"[:60]
            return await self.fallback_battery(reason, command_id=command_id)

        economic = intent.direction in {ControlDirection.CHARGE, ControlDirection.DISCHARGE}
        soc_pct, soc_age = self._latest_soc_with_age(now)
        # Watchdog health is proven in Task 11; fail closed until then.
        safety = evaluate(
            SafetyInputs(
                telemetry_stale=False,
                telemetry_stale_reasons=[],
                have_current_price=True,
                have_pv_forecast=True,
                have_load_forecast=True,
                known_price_hours=float(s.optimise_horizon_hours),
                horizon_hours=float(s.optimise_horizon_hours),
                mode_is_control=s.mode == "control",
                battery_control_enabled=s.battery_control_enabled,
                number_register_ack_reliable=s.battery_control_number_register_ack_reliable,
                lease_held=True,
                watchdog_healthy=False,
                economic_action=economic,
                plan_status_ok=True,
                plan_age_seconds=plan_age,
                max_plan_age_seconds=s.battery_control_max_plan_age_seconds,
                max_telemetry_age_seconds=s.battery_control_max_telemetry_age_seconds,
                current_interval_start=interval_start,
                current_interval_end=interval_start + dt.timedelta(minutes=s.step_minutes),
                now=now,
                current_buy_price=None,
                current_sell_price=None,
                current_price_is_real=False,
                current_price_age_seconds=None,
                soc_pct=soc_pct,
                soc_update_age_seconds=soc_age,
                corroborating_power_fresh=False,
            )
        )
        with self.store.session() as session:
            persist_pending_action(
                session,
                command_id=command_id,
                source_run_id=run_id,
                interval_start=interval_start,
                intent={
                    "direction": intent.direction.value,
                    "requested_power_kw": intent.requested_power_kw,
                    "grid_charge": intent.grid_charge,
                    "export": intent.export,
                    "reason_codes": list(intent.reason_codes),
                },
                authorization_allowed=safety.control_authorized,
                blockers=safety.control_blockers,
                requested_state=intent.direction.value,
            )

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
            number_register_ack_reliable=s.battery_control_number_register_ack_reliable,
        ) as ha:
            controller = SigenergyController(ha, s)
            outcome = await controller.apply_intent(intent)

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
        from .control_store import ensure_controller_state, finalize_action, persist_pending_action
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

        if s.mode != "control" or not s.battery_control_enabled or not s.ha_token:
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
            return {"result": "fallback_recorded", "command_id": command_id, "reason": reason}

        async with HaClient(
            s.ha_url,
            s.ha_token,
            verify_ssl=s.ha_verify_ssl,
            number_register_ack_reliable=s.battery_control_number_register_ack_reliable,
        ) as ha:
            outcome = await SigenergyController(ha, s).fallback(reason, command_id=command_id)

        with self.store.session() as session:
            finalize_action(
                session,
                command_id,
                observed_state=(
                    outcome.control.observed_state.value
                    if outcome.control.observed_state
                    else "FALLBACK"
                ),
                physical={"verified": outcome.control.physical_verified},
                result="fallback",
                error_code=reason,
                latency_ms=outcome.control.latency_ms,
            )
            state = ensure_controller_state(session)
            state.last_fallback_at = utcnow()
            state.last_fallback_verified = bool(outcome.control.physical_verified)
            state.state = (
                outcome.control.observed_state.value
                if outcome.control.observed_state
                else "FALLBACK"
            )
        return {"result": "fallback", "command_id": command_id, "reason": reason}

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

        if s.mode != "control" or not s.battery_control_enabled or not s.ha_token:
            return {"result": "disarmed_startup"}

        async with HaClient(s.ha_url, s.ha_token, verify_ssl=s.ha_verify_ssl) as ha:
            remote = await ha.get_state(s.battery_control_remote_ems_switch_entity)
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
            armed = (
                s.mode == "control"
                and s.battery_control_enabled
                and bool(s.battery_control_arm_token)
            )
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

    async def refresh_prices(self, days_ahead: int = 2, history_days: int | None = None) -> int:
        s = self.settings
        if not s.pstryk_api_key:
            logger.debug("No Pstryk key configured; skipping price refresh")
            return 0
        now = utcnow()
        start = now.replace(minute=0, second=0, microsecond=0)
        if history_days is not None:
            start = start - dt.timedelta(days=history_days)
        end = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(days=days_ahead)
        async with PstrykClient(s.pstryk_api_key, s.pstryk_base_url) as client:
            frames = await client.fetch_pricing(start, end)
        count = 0
        with self.store.session() as session:
            for fr in frames:
                session.merge(
                    Price(
                        interval_start=fr.interval_start,
                        tge=fr.tge,
                        service=fr.service,
                        distribution=fr.distribution,
                        excise=fr.excise,
                        vat=fr.vat,
                        base=fr.base,
                        buy_gross=fr.buy_gross,
                        full_price=fr.full_price,
                        sell_gross=fr.sell_gross,
                        is_cheap=fr.is_cheap,
                        is_expensive=fr.is_expensive,
                        source="api",
                        fetched_at=now,
                    )
                )
                count += 1
        logger.info("Refreshed %d price frames", count)
        return count

    async def refresh_meter_values(self, days_back: int = 7) -> int:
        """Reconcile complete Pstryk billing intervals in the refreshed window."""
        s = self.settings
        if not s.pstryk_api_key:
            logger.debug("No Pstryk key configured; skipping meter refresh")
            return 0
        now = utcnow()
        end = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        start = end - dt.timedelta(days=max(1, days_back))
        async with PstrykClient(s.pstryk_api_key, s.pstryk_base_url) as client:
            frames = await client.fetch_meter_values(start, end, resolution="hour")
        fetched_at = utcnow()
        accepted_starts = [frame.interval_start for frame in frames]
        with self.store.session() as session:
            stale_rows = (
                delete(PstrykMeterInterval)
                .where(PstrykMeterInterval.interval_start >= start)
                .where(PstrykMeterInterval.interval_end <= now)
            )
            if accepted_starts:
                stale_rows = stale_rows.where(
                    PstrykMeterInterval.interval_start.not_in(accepted_starts)
                )
            session.execute(stale_rows)
            for frame in frames:
                session.merge(
                    PstrykMeterInterval(
                        interval_start=frame.interval_start,
                        interval_end=frame.interval_end,
                        import_kwh=frame.import_kwh,
                        export_kwh=frame.export_kwh,
                        balance_kwh=frame.balance_kwh,
                        resolution="hour",
                        source="pstryk",
                        fetched_at=fetched_at,
                    )
                )
        logger.info("Refreshed %d authoritative Pstryk meter intervals", len(frames))
        return len(frames)

    async def bootstrap(self) -> None:
        """One-shot startup backfill so backtests and the price chart have history
        immediately instead of only after hours/days of live collection."""
        days = self.settings.pstryk_history_bootstrap_days
        if days > 0:
            try:
                await self.refresh_prices(history_days=days)
            except Exception:  # pragma: no cover - network dependent
                logger.exception("price history bootstrap failed")
            try:
                await self.refresh_meter_values(days_back=days)
            except Exception:  # pragma: no cover - network dependent
                logger.exception("Pstryk meter history bootstrap failed")
        try:
            await self.bootstrap_telemetry_history(days)
        except Exception:  # pragma: no cover - network dependent
            logger.exception("telemetry history bootstrap failed")

    async def bootstrap_telemetry_history(self, days: int) -> int:
        """Backfill hourly telemetry from the Home Assistant recorder so backtests have real
        PV/load history. Idempotent: skipped when telemetry already reaches back far enough."""
        s = self.settings
        if not s.ha_token or days <= 0:
            return 0
        now = utcnow()
        start = (now - dt.timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
        completed_end = now.replace(minute=0, second=0, microsecond=0)
        with self.store.session() as session:
            probe = (
                session.execute(
                    select(Telemetry.ts)
                    .where(Telemetry.ts >= start)
                    .where(Telemetry.ts < start + dt.timedelta(hours=1))
                    .limit(4)
                )
                .scalars()
                .all()
            )
        if len(probe) >= 4:
            logger.info("Coverage-capable telemetry history already present; skipping bootstrap")
            return 0
        entities = {
            "soc_pct": ENTITY_SOC,
            "pv_kw": ENTITY_PV_POWER,
            "load_kw": ENTITY_CONSUMED_POWER,
            "grid_import_kw": ENTITY_GRID_IMPORT_POWER,
            "grid_export_kw": ENTITY_GRID_EXPORT_POWER,
            "batt_power_kw": ENTITY_BATTERY_POWER,
        }
        histories: dict[str, dict[dt.datetime, float]] = {}
        async with HaClient(s.ha_url, s.ha_token, verify_ssl=s.ha_verify_ssl) as ha:
            for field, eid in entities.items():
                try:
                    states = await ha.get_history(eid, start, completed_end)
                except Exception:  # pragma: no cover - network dependent
                    logger.warning("history fetch failed for %s", eid, exc_info=True)
                    states = []
                histories[field] = _regular_state_samples(states, start, completed_end)

        ticks = sorted({tick for samples in histories.values() for tick in samples})
        count = 0
        with self.store.session() as session:
            for tick in ticks:
                charge_kw, discharge_kw = _split_battery_power(histories["batt_power_kw"].get(tick))
                session.merge(
                    Telemetry(
                        ts=tick,
                        soc_pct=histories["soc_pct"].get(tick),
                        pv_kw=histories["pv_kw"].get(tick),
                        load_kw=histories["load_kw"].get(tick),
                        grid_import_kw=histories["grid_import_kw"].get(tick),
                        grid_export_kw=histories["grid_export_kw"].get(tick),
                        batt_charge_kw=charge_kw,
                        batt_discharge_kw=discharge_kw,
                        stale=False,
                    )
                )
                count += 1
        logger.info("Bootstrapped %d five-minute telemetry samples", count)
        return count

    async def run_optimise(self) -> str:
        """Build inputs, solve, evaluate safety, persist an audit record, publish MQTT."""
        s = self.settings
        now = utcnow()
        run_id = uuid.uuid4().hex

        soc_start_pct = self._latest_soc_pct()
        telemetry_stale, stale_reasons = self._telemetry_stale(now)
        prices = self._future_prices(now)
        have_current_price = self._have_current_price(prices, now)
        known_hours = self._known_price_hours(prices, now)

        pv_map, load_map, pv_conf, load_conf = await self._forecast_maps_live(now, prices)
        forecast_surplus_kwh = same_day_forecast_surplus_kwh(
            now,
            pv_map,
            load_map,
            tz=s.tz,
            factor=s.ev_forecast_surplus_factor,
        )
        battery_target_kwh = s.battery_capacity_kwh * s.ev_battery_full_soc_pct / 100.0
        battery_fill_input_kwh = max(
            0.0,
            battery_target_kwh
            - (
                _soc_pct_or_reserve(soc_start_pct, s.battery_soc_min_pct)
                / 100.0
                * s.battery_capacity_kwh
            ),
        ) / max(s.eta_charge, 1e-6)
        # Optional EV energy is backed only by surplus left after the
        # stationary battery's projected path to its full target.
        ev_forecast_surplus_kwh = max(0.0, forecast_surplus_kwh - battery_fill_input_kwh)
        ev_live = self._latest_ev_telemetry()
        ev_row_fresh = bool(
            ev_live is not None and (now - _aware(ev_live.ts)).total_seconds() <= 10 * 60
        )
        ev_soc_trustworthy = bool(
            ev_row_fresh and ev_live is not None and ev_live.soc_pct is not None
        )
        ev_available = bool(
            ev_row_fresh
            and ev_live is not None
            and not ev_live.stale
            and not ev_live.fault
            and ev_live.soc_pct is not None
            and ev_live.soc_pct < EV_FULL_TARGET_SOC_PCT
            and ev_live.charging_status
            in (
                s.ev_active_charging_statuses if ev_live.switch_on else s.ev_start_charging_statuses
            )
        )
        candidate_intervals = self._build_intervals(
            prices,
            pv_map,
            load_map,
            now=now,
            ev_available=ev_available,
        )
        interval_starts = [dt.datetime.fromisoformat(i.interval_start) for i in candidate_intervals]
        max_opportunistic_slots = sum(i.ev_opportunistic_allowed for i in candidate_intervals)
        ev_requirements = (
            build_ev_requirements(
                ev_live.soc_pct,
                now,
                interval_starts if ev_available else [],
                s,
                forecast_surplus_kwh=ev_forecast_surplus_kwh,
                max_opportunistic_slots=max_opportunistic_slots,
            )
            if ev_soc_trustworthy and ev_live is not None and ev_live.soc_pct is not None
            else EvRequirements(now, 0, 0)
        )
        intervals = self._build_intervals(
            prices,
            pv_map,
            load_map,
            now=now,
            ev_available=ev_available,
            ev_departure_at=ev_requirements.departure_at,
        )
        have_pv = bool(pv_map)
        have_load = bool(load_map)

        safety = evaluate(
            SafetyInputs(
                telemetry_stale=telemetry_stale,
                telemetry_stale_reasons=stale_reasons,
                have_current_price=have_current_price,
                have_pv_forecast=have_pv,
                have_load_forecast=have_load,
                known_price_hours=known_hours,
                horizon_hours=float(s.optimise_horizon_hours),
                mode_is_control=s.mode == "control",
                battery_control_enabled=s.battery_control_enabled,
                number_register_ack_reliable=s.battery_control_number_register_ack_reliable,
                max_plan_age_seconds=s.battery_control_max_plan_age_seconds,
                max_telemetry_age_seconds=s.battery_control_max_telemetry_age_seconds,
                economic_action=False,  # planning path; live control authorization is separate
            )
        )
        _apply_ev_shortfall_warning(safety, ev_requirements, s.step_minutes)

        params = self.optimiser_params(ev_requirements)
        soc_start_kwh = (
            _soc_pct_or_reserve(soc_start_pct, s.battery_soc_min_pct)
            / 100.0
            * s.battery_capacity_kwh
        )

        objective = None
        solve_ms = 0.0
        steps = []
        status = safety.status
        if intervals and safety.status != Status.BLOCKED:
            result = optimise(intervals, soc_start_kwh, params)
            objective = result.objective_pln
            solve_ms = result.solve_ms
            steps = result.steps
            if result.status != "optimal":
                status = Status.BLOCKED
                safety.blockers.append(f"solver status: {result.status}")

        solver_input = self._solver_input_snapshot(intervals, soc_start_kwh, params)
        blob = json.dumps(solver_input, sort_keys=True, default=str)
        sha = hashlib.sha256(blob.encode()).hexdigest()

        decision = classify_next_action(
            steps,
            buy_price=intervals[0].buy_price if intervals else None,
            sell_price=intervals[0].sell_price if intervals else None,
            future_max_buy=max((i.buy_price for i in intervals), default=None),
            future_max_sell=max((i.sell_price for i in intervals), default=None),
        )

        with self.store.session() as session:
            # Only the latest run's forecasts are ever read back; replace them each run so
            # the audit table stays bounded instead of growing every 15 minutes.
            session.execute(delete(Forecast))
            for hour, energy in _hourly_from_map(pv_map).items():
                session.add(
                    Forecast(
                        run_id=run_id,
                        interval_start=hour,
                        kind="pv",
                        value=energy,
                        confidence=pv_conf or "low_confidence",
                    )
                )
            for hour, energy in _hourly_from_map(load_map).items():
                session.add(
                    Forecast(
                        run_id=run_id,
                        interval_start=hour,
                        kind="load",
                        value=energy,
                        confidence=load_conf or "low_confidence",
                    )
                )
            session.add(
                Run(
                    run_id=run_id,
                    ts=now,
                    mode=s.mode,
                    horizon_hours=float(s.optimise_horizon_hours),
                    known_price_hours=known_hours,
                    input_state=json.dumps({"soc_pct": soc_start_pct}),
                    solver_input=blob,
                    solver_input_schema=SOLVER_INPUT_SCHEMA,
                    solver_input_sha256=sha,
                    objective_pln=objective,
                    status=status.value,
                    reason=decision.reason,
                    safety=json.dumps(safety.as_dict()),
                    solve_ms=solve_ms,
                )
            )
            for step in steps:
                step_start = (
                    dt.datetime.fromisoformat(step.interval_start)
                    if _is_iso(step.interval_start)
                    else now
                )
                session.add(
                    PlanStep(
                        run_id=run_id,
                        interval_start=step_start,
                        dt_hours=step.dt_hours,
                        # Persist EV source flows in the legacy aggregate `*_to_load`
                        # columns so stored plans remain energy-balanced without a DB
                        # migration. The optimiser result keeps the sources separate.
                        pv_to_load_kwh=step.pv_to_load_kwh + step.pv_to_ev_kwh,
                        pv_to_battery_kwh=step.pv_to_battery_kwh,
                        pv_to_grid_kwh=step.pv_to_grid_kwh,
                        grid_to_load_kwh=step.grid_to_load_kwh + step.grid_to_ev_kwh,
                        grid_to_battery_kwh=step.grid_to_battery_kwh,
                        battery_to_load_kwh=step.battery_to_load_kwh + step.battery_to_ev_kwh,
                        battery_to_grid_kwh=step.battery_to_grid_kwh,
                        curtail_kwh=step.curtail_kwh,
                        soc_pct_end=step.soc_pct_end,
                        marginal_value=step.marginal_value,
                    )
                )
                session.add(
                    EvPlanStep(
                        run_id=run_id,
                        interval_start=step_start,
                        charge_kwh=step.ev_charge_kwh,
                        planned_on=step.ev_charge_kwh > 1e-6,
                    )
                )

        self._publish_recommendation(decision, status, objective)
        logger.info("Optimise run %s status=%s objective=%s", run_id, status.value, objective)
        return run_id

    def optimiser_params(self, ev: EvRequirements | None = None) -> OptimiserParams:
        s = self.settings
        return OptimiserParams(
            battery_capacity_kwh=s.battery_capacity_kwh,
            soc_min_kwh=s.soc_min_kwh,
            battery_hard_min_kwh=s.hard_soc_min_kwh,
            soc_max_kwh=s.soc_max_kwh,
            max_charge_kw=s.battery_max_charge_kw,
            max_discharge_kw=s.battery_max_discharge_kw,
            eta_charge=s.eta_charge,
            eta_discharge=s.eta_discharge,
            site_import_limit_kw=s.site_import_limit_kw,
            site_export_limit_kw=s.site_export_limit_kw,
            inverter_limit_kw=s.inverter_limit_kw,
            degradation_cost_pln_per_kwh=s.degradation_cost_pln_per_kwh,
            import_price_adjustment_pln_kwh=s.import_price_adjustment_pln_kwh,
            allow_battery_export=s.allow_battery_export,
            allow_grid_charging=s.allow_grid_charging,
            activation_margin_pln_kwh=s.battery_control_activation_margin_pln_kwh,
            grid_charge_margin_pln_kwh=s.grid_charge_margin_pln_kwh,
            minimum_export_spread_pln_kwh=s.minimum_export_spread_pln_kwh,
            terminal_soc_salvage_pln_kwh=s.terminal_soc_salvage_pln_kwh,
            ev_charge_power_kw=s.ev_charge_power_kw if ev else 0.0,
            ev_target_slots=ev.target_slots if ev else 0,
            ev_minimum_slots=ev.minimum_slots if ev else 0,
            ev_opportunistic_terminal_soc_kwh=(
                s.battery_capacity_kwh * s.ev_battery_full_soc_pct / 100.0
            ),
        )

    # --- helpers -----------------------------------------------------------
    def _latest_soc_pct(self) -> float | None:
        soc, _age = self._latest_soc_with_age(utcnow())
        return soc

    def _latest_soc_with_age(self, now: dt.datetime) -> tuple[float | None, float | None]:
        with self.store.session() as session:
            row = session.execute(
                select(Telemetry).order_by(Telemetry.ts.desc()).limit(1)
            ).scalar_one_or_none()
        if row is None:
            return None, None
        return row.soc_pct, max(0.0, (now - _aware(row.ts)).total_seconds())

    def _latest_ev_telemetry(self) -> EvTelemetry | None:
        with self.store.session() as session:
            return session.execute(
                select(EvTelemetry).order_by(EvTelemetry.ts.desc()).limit(1)
            ).scalar_one_or_none()

    def _telemetry_stale(self, now: dt.datetime) -> tuple[bool, list[str]]:
        with self.store.session() as session:
            row = session.execute(
                select(Telemetry).order_by(Telemetry.ts.desc()).limit(1)
            ).scalar_one_or_none()
        if row is None:
            return True, ["no telemetry collected yet"]
        age = (now - _aware(row.ts)).total_seconds()
        if age > 600:
            return True, [f"latest telemetry is {age / 60:.0f} min old"]
        return bool(row.stale), (["telemetry flagged stale"] if row.stale else [])

    def _future_prices(self, now: dt.datetime) -> list[Price]:
        floor = now.replace(minute=0, second=0, microsecond=0)
        horizon_end = floor + dt.timedelta(hours=self.settings.optimise_horizon_hours)
        with self.store.session() as session:
            rows = (
                session.execute(
                    select(Price)
                    .where(Price.interval_start >= floor)
                    .where(Price.interval_start < horizon_end)
                    .order_by(Price.interval_start)
                )
                .scalars()
                .all()
            )
        return list(rows)

    def _have_current_price(self, prices: list[Price], now: dt.datetime) -> bool:
        floor = now.replace(minute=0, second=0, microsecond=0)
        return any(_aware(p.interval_start) == floor and p.buy_gross is not None for p in prices)

    def _known_price_hours(self, prices: list[Price], now: dt.datetime) -> float:
        floor = now.replace(minute=0, second=0, microsecond=0)
        hours = 0.0
        expected = floor
        for p in sorted(prices, key=lambda x: x.interval_start):
            if _aware(p.interval_start) == expected and p.buy_gross is not None:
                hours += 1.0
                expected = expected + dt.timedelta(hours=1)
        return hours

    def _interval_grid(
        self, prices: list[Price], now: dt.datetime | None = None
    ) -> list[tuple[dt.datetime, float]]:
        """Expand hourly prices to aligned future sub-hour intervals."""
        step_h = self.settings.step_hours
        substeps = max(1, int(round(1.0 / step_h)))
        first_start = _next_interval_start(now, self.settings.step_minutes) if now else None
        grid: list[tuple[dt.datetime, float]] = []
        for p in sorted(prices, key=lambda x: x.interval_start):
            if p.buy_gross is None:
                continue
            hour_start = _aware(p.interval_start)
            for k in range(substeps):
                start = hour_start + dt.timedelta(hours=step_h * k)
                if first_start is None or start >= first_start:
                    grid.append((start, step_h))
        return grid

    def _build_intervals(
        self,
        prices: list[Price],
        pv_map: dict[dt.datetime, float],
        load_map: dict[dt.datetime, float],
        *,
        now: dt.datetime | None = None,
        ev_available: bool = False,
        ev_departure_at: dt.datetime | None = None,
    ) -> list[IntervalInput]:
        """Expand prices and attach forecasts plus EV availability/deadline flags."""
        step_h = self.settings.step_hours
        substeps = max(1, int(round(1.0 / step_h)))
        first_start = _next_interval_start(now, self.settings.step_minutes) if now else None
        intervals: list[IntervalInput] = []
        for p in sorted(prices, key=lambda x: x.interval_start):
            if p.buy_gross is None:
                continue
            hour_start = _aware(p.interval_start)
            for k in range(substeps):
                start = hour_start + dt.timedelta(hours=step_h * k)
                if first_start is not None and start < first_start:
                    continue
                intervals.append(
                    IntervalInput(
                        interval_start=start.isoformat(),
                        dt_hours=step_h,
                        pv_energy_kwh=pv_map.get(start, 0.0),
                        load_energy_kwh=load_map.get(start, 0.0),
                        buy_price=float(p.buy_gross),
                        sell_price=float(p.sell_gross or 0.0),
                        price_is_real=(p.source == "api"),
                        ev_available=ev_available,
                        ev_required_soon=bool(
                            ev_available and ev_departure_at and start < ev_departure_at
                        ),
                        ev_opportunistic_allowed=bool(
                            p.is_expensive is not True
                            and (
                                now is None
                                or start.astimezone(ZoneInfo(self.settings.tz)).date()
                                == now.astimezone(ZoneInfo(self.settings.tz)).date()
                            )
                        ),
                    )
                )
        return intervals

    async def _forecast_maps_live(
        self, now: dt.datetime, prices: list[Price]
    ) -> tuple[dict[dt.datetime, float], dict[dt.datetime, float], str | None, str | None]:
        """Compute PV and load forecasts on the optimiser's interval grid (in-memory).

        PV comes from the configured provider (Forecast.Solar); load from the rolling
        hour-of-day/weekday median of stored telemetry. Both return empty when their
        inputs are unavailable so safety can flag the run low-confidence.
        """
        grid = self._interval_grid(prices, now)
        pv_map, pv_conf = await self._pv_forecast_map(grid)
        load_map, load_conf = self._load_forecast_map(now, grid)
        return pv_map, load_map, pv_conf, load_conf

    async def _pv_forecast_map(
        self, grid: list[tuple[dt.datetime, float]]
    ) -> tuple[dict[dt.datetime, float], str | None]:
        s = self.settings
        if s.pv_forecast_provider == "none" or not s.pv_planes or not grid:
            return {}, None
        try:
            async with PvForecaster(
                s.pv_lat,
                s.pv_lon,
                s.pv_planes,
                provider=s.pv_forecast_provider,
                solcast_api_key=s.solcast_api_key,
            ) as pvf:
                points = await pvf.forecast()
        except Exception:  # pragma: no cover - network dependent
            logger.warning("PV forecast failed", exc_info=True)
            return {}, None
        if not points:
            return {}, None
        hourly = {_aware(p.interval_start): p.energy_kwh for p in points}
        conf = "ok" if all(p.confidence == "ok" for p in points) else "low_confidence"
        # Distribute each hour's energy across its sub-hour steps proportionally to dt.
        out: dict[dt.datetime, float] = {}
        for start, dt_hours in grid:
            hour = start.replace(minute=0, second=0, microsecond=0)
            energy = hourly.get(hour)
            if energy is not None:
                out[start] = energy * dt_hours
        return out, conf

    def _load_forecast_map(
        self, now: dt.datetime, grid: list[tuple[dt.datetime, float]]
    ) -> tuple[dict[dt.datetime, float], str | None]:
        samples = self._load_samples(now)
        if not samples or not grid:
            return {}, None
        points = LoadForecaster(tz=self.settings.tz, lookback_days=LOAD_LOOKBACK_DAYS).forecast(
            samples, grid
        )
        out = {_aware(p.interval_start): p.load_kwh for p in points}
        conf = "ok" if all(p.confidence == "ok" for p in points) else "low_confidence"
        return out, conf

    def _load_samples(self, now: dt.datetime) -> list[LoadSample]:
        """Hourly load history, reconciled to Pstryk's settlement boundary when available."""
        lookback = now - dt.timedelta(days=LOAD_LOOKBACK_DAYS)
        with self.store.session() as session:
            rows = (
                session.execute(
                    select(Telemetry).where(Telemetry.ts >= lookback).order_by(Telemetry.ts)
                )
                .scalars()
                .all()
            )
            meter_rows = (
                session.execute(
                    select(PstrykMeterInterval)
                    .where(PstrykMeterInterval.interval_start >= lookback)
                    .where(PstrykMeterInterval.interval_start < now)
                )
                .scalars()
                .all()
            )
        meter_by_hour = {
            _aware(row.interval_start).replace(minute=0, second=0, microsecond=0): row
            for row in meter_rows
        }
        energy_by_hour = complete_hourly_energy(rows)

        samples: list[LoadSample] = []
        for hour, energy in sorted(energy_by_hour.items()):
            meter = meter_by_hour.get(hour)
            if meter is None:
                continue

            # Settlement-boundary balance. Pstryk import/export replace Sigen's grid
            # channels; Sigen remains the source for covered PV and battery energy.
            corrected_load = max(
                0.0,
                energy["pv"]
                + meter.import_kwh
                + energy["discharge"]
                - meter.export_kwh
                - energy["charge"],
            )
            samples.append(LoadSample(ts=hour, load_kw=corrected_load))
        return samples

    def _solver_input_snapshot(
        self, intervals: list[IntervalInput], soc_start_kwh: float, params: OptimiserParams
    ) -> dict[str, object]:
        return {
            "schema": SOLVER_INPUT_SCHEMA,
            "soc_start_kwh": soc_start_kwh,
            "params": asdict(params),
            "intervals": [asdict(i) for i in intervals],
        }

    def _publish_recommendation(
        self,
        decision,
        status: Status,
        objective: float | None,  # noqa: ANN001
    ) -> None:
        if self._mqtt is None:
            return
        try:
            self._mqtt.publish_state(
                RecommendationState(
                    next_action=decision.action,
                    next_action_power_kw=decision.power_kw,
                    target_soc=decision.target_soc_pct,
                    expected_profit_today=-(objective or 0.0),
                    actual_cost_today=0.0,
                    missed_opportunity_today=0.0,
                    decision_reason=decision.reason,
                    confidence=status.value,
                    control_enabled=CONTROL_ENABLED,
                )
            )
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("MQTT publish failed: %s", exc)


def _soc_pct_or_reserve(soc_pct: float | None, reserve_pct: float) -> float:
    return reserve_pct if soc_pct is None else soc_pct


def _ev_live_state(ev: EvTelemetry | None, now: dt.datetime) -> EvLiveState:
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


def _relay_failure_backoff_decision(
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


async def _apply_ev_relay_decision(
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
        if await _verify_ev_relay_state(
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

    return await _force_ev_relay_off(
        ha,
        entity_id,
        failure,
        settle_seconds=settle_seconds,
        verify_interval_seconds=verify_interval_seconds,
        verify_timeout_seconds=verify_timeout_seconds,
    )


async def _force_ev_relay_off(
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

    off_confirmed = await _verify_ev_relay_state(
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


async def _verify_ev_relay_state(
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
            actual = _state_bool(await ha.get_state(entity_id))
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


def _apply_ev_shortfall_warning(
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


def _hourly_from_map(values: dict[dt.datetime, float]) -> dict[dt.datetime, float]:
    """Aggregate a sub-hour-step map into hourly sums for compact forecast persistence."""
    out: dict[dt.datetime, float] = {}
    for ts, value in values.items():
        hour = _aware(ts).replace(minute=0, second=0, microsecond=0)
        out[hour] = out.get(hour, 0.0) + value
    return out


def _regular_state_samples(
    states: list[HaState], start: dt.datetime, end: dt.datetime
) -> dict[dt.datetime, float]:
    """Resample recorder states every five minutes using time-correct hold semantics."""
    points = sorted(
        (
            (_aware(timestamp), state.as_float())
            for state in states
            if (timestamp := state.last_updated) is not None
        ),
        key=lambda item: item[0],
    )
    if not points:
        return {}

    samples: dict[dt.datetime, float] = {}
    tick = _aware(start)
    end = _aware(end)
    index = 0
    current: float | None = None
    while tick < end:
        while index < len(points) and points[index][0] <= tick:
            current = points[index][1]
            index += 1
        if current is not None:
            samples[tick] = current
        tick += dt.timedelta(minutes=5)
    return samples


def _next_interval_start(value: dt.datetime, step_minutes: int) -> dt.datetime:
    value = _aware(value)
    floor = value.replace(
        minute=(value.minute // step_minutes) * step_minutes,
        second=0,
        microsecond=0,
    )
    # Scheduler runs on quarter-hour boundaries; tolerate startup/call latency so a
    # run a few seconds late still controls the just-started slot.
    if (value - floor).total_seconds() <= 60:
        return floor
    return floor + dt.timedelta(minutes=step_minutes)


def _ev_fault_status(states: list[HaState | None]) -> tuple[bool, bool]:
    """Treat any missing/unavailable configured protection signal as a fault."""
    values = [_state_bool(state) for state in states]
    unavailable = any(value is None for value in values)
    return (unavailable or any(value is True for value in values), unavailable)


def _state_bool(state: HaState | None) -> bool | None:
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


def _is_iso(value: str) -> bool:
    try:
        dt.datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False
