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
import uuid

from sqlalchemy import delete, select

from .battery_loop import BatteryMixin
from .config import Settings
from .ev import (
    EV_FULL_TARGET_SOC_PCT,
    EvRequirements,
    apply_ev_shortfall_warning,
    build_ev_requirements,
    same_day_forecast_surplus_kwh,
)
from .ev_control import (
    EvControlDecision,
    apply_ev_relay_decision,
    decide_ev_control,
    ev_fault_status,
    ev_live_state,
    relay_failure_backoff_decision,
    state_bool,
)
from .explain import classify_next_action
from .ha_client import (
    ENTITY_BATTERY_POWER,
    ENTITY_CONSUMED_POWER,
    ENTITY_GRID_EXPORT_POWER,
    ENTITY_GRID_IMPORT_POWER,
    ENTITY_PV_POWER,
    ENTITY_SOC,
    HaClient,
    _split_battery_power,
)
from .mqtt_publish import MqttConfig, MqttPublisher, RecommendationState
from .optimiser import OptimiserParams, optimise
from .planning import (
    SOLVER_INPUT_SCHEMA,
    PlanningMixin,
    has_complete_telemetry_coverage,
    hourly_from_map,
    regular_state_samples,
    soc_pct_or_reserve,
)
from .pstryk_client import PstrykClient
from .safety import SafetyInputs, Status, evaluate
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
from .watchdog import watchdog_health_from_ha as watchdog_health_from_ha

logger = logging.getLogger(__name__)


class Service(BatteryMixin, PlanningMixin):
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._mqtt: MqttPublisher | None = None
        self.ev_charge_to_100_active = False
        self.controller_owner_id = str(uuid.uuid4())
        self._battery_control_lock = asyncio.Lock()
        self._manual_command = None

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
        fault, fault_stale = ev_fault_status(fault_states)
        self.ev_charge_to_100_active = state_bool(charge_to_100) is True
        switch_on = state_bool(switch)
        soc_pct = soc.as_float() if soc else None
        charging_status = (
            status.state if status and status.state not in {"unknown", "unavailable"} else None
        )
        power_w = power.as_float() if power else None
        row = EvTelemetry(
            ts=utcnow(),
            soc_pct=soc_pct,
            charging_status=charging_status,
            charging_active=state_bool(active),
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
        live = ev_live_state(ev, now)
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
        decision = relay_failure_backoff_decision(
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
                        decision = await apply_ev_relay_decision(
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
            coverage_complete = has_complete_telemetry_coverage(session, start, completed_end)
        if coverage_complete:
            logger.info("Complete telemetry history already present; skipping bootstrap")
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
                histories[field] = regular_state_samples(states, start, completed_end)

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
                soc_pct_or_reserve(soc_start_pct, s.battery_soc_min_pct)
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
        have_pv = bool(pv_map) and pv_conf == "ok"
        have_load = bool(load_map) and load_conf == "ok"
        # Intervals exist only where Pstryk has published, so this is the horizon the
        # solver actually saw — not the configured fetch bound.
        effective_horizon_hours = sum(i.dt_hours for i in intervals)

        safety = evaluate(
            SafetyInputs(
                telemetry_stale=telemetry_stale,
                telemetry_stale_reasons=stale_reasons,
                have_current_price=have_current_price,
                have_pv_forecast=have_pv,
                have_load_forecast=have_load,
                known_price_hours=known_hours,
                horizon_hours=effective_horizon_hours,
                min_price_hours=s.optimise_min_price_hours,
                mode_is_control=s.mode == "control",
                battery_control_enabled=s.battery_control_enabled,
                max_plan_age_seconds=s.battery_control_max_plan_age_seconds,
                max_telemetry_age_seconds=s.battery_control_max_telemetry_age_seconds,
                economic_action=False,  # planning path; live control authorization is separate
            )
        )
        apply_ev_shortfall_warning(safety, ev_requirements, s.step_minutes)

        params = self.optimiser_params(ev_requirements)
        soc_start_kwh = (
            soc_pct_or_reserve(soc_start_pct, s.battery_soc_min_pct)
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
            for hour, energy in hourly_from_map(pv_map).items():
                session.add(
                    Forecast(
                        run_id=run_id,
                        interval_start=hour,
                        kind="pv",
                        value=energy,
                        confidence=pv_conf or "low_confidence",
                    )
                )
            for hour, energy in hourly_from_map(load_map).items():
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
                    horizon_hours=effective_horizon_hours,
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
            terminal_soc_salvage_auto=s.terminal_soc_salvage_auto,
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
                    control_enabled=self.settings.battery_actuation_live,
                )
            )
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("MQTT publish failed: %s", exc)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _is_iso(value: str) -> bool:
    try:
        dt.datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False
