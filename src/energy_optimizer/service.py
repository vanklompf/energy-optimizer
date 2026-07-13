"""Application service: orchestrates collection, optimisation and publishing.

Holds the long-lived dependencies (settings, store, MQTT publisher) and exposes the unit
jobs the scheduler calls. Each optimise run writes an auditable ``runs`` + ``plan_steps``
record including an immutable, hashed ``solver_input`` snapshot.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from dataclasses import asdict

from sqlalchemy import delete, select

from .config import Settings
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
from .safety import CONTROL_ENABLED, SafetyInputs, Status, evaluate
from .store import Forecast, PlanStep, Price, Run, Store, Telemetry, utcnow

LOAD_LOOKBACK_DAYS = 28

logger = logging.getLogger(__name__)

SOLVER_INPUT_SCHEMA = "1"


class Service:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._mqtt: MqttPublisher | None = None

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
        start = now - dt.timedelta(days=days)
        with self.store.session() as session:
            earliest = session.execute(
                select(Telemetry.ts).order_by(Telemetry.ts).limit(1)
            ).scalar_one_or_none()
        if earliest is not None and _aware(earliest) <= start + dt.timedelta(hours=1):
            logger.info("Telemetry history already present; skipping bootstrap")
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
                    states = await ha.get_history(eid, start, now)
                except Exception:  # pragma: no cover - network dependent
                    logger.warning("history fetch failed for %s", eid, exc_info=True)
                    states = []
                histories[field] = _hourly_mean_states(states)
        hours = sorted({h for m in histories.values() for h in m})
        count = 0
        with self.store.session() as session:
            for hour in hours:
                charge_kw, discharge_kw = _split_battery_power(histories["batt_power_kw"].get(hour))
                session.merge(
                    Telemetry(
                        ts=hour,
                        soc_pct=histories["soc_pct"].get(hour),
                        pv_kw=histories["pv_kw"].get(hour),
                        load_kw=histories["load_kw"].get(hour),
                        grid_import_kw=histories["grid_import_kw"].get(hour),
                        grid_export_kw=histories["grid_export_kw"].get(hour),
                        batt_charge_kw=charge_kw,
                        batt_discharge_kw=discharge_kw,
                        stale=False,
                    )
                )
                count += 1
        logger.info("Bootstrapped %d hours of telemetry history", count)
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
        intervals = self._build_intervals(prices, pv_map, load_map)
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
            )
        )

        params = self.optimiser_params()
        soc_start_kwh = (soc_start_pct or s.battery_soc_min_pct) / 100.0 * s.battery_capacity_kwh

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
                session.add(
                    PlanStep(
                        run_id=run_id,
                        interval_start=dt.datetime.fromisoformat(step.interval_start)
                        if _is_iso(step.interval_start)
                        else now,
                        dt_hours=step.dt_hours,
                        pv_to_load_kwh=step.pv_to_load_kwh,
                        pv_to_battery_kwh=step.pv_to_battery_kwh,
                        pv_to_grid_kwh=step.pv_to_grid_kwh,
                        grid_to_load_kwh=step.grid_to_load_kwh,
                        grid_to_battery_kwh=step.grid_to_battery_kwh,
                        battery_to_load_kwh=step.battery_to_load_kwh,
                        battery_to_grid_kwh=step.battery_to_grid_kwh,
                        curtail_kwh=step.curtail_kwh,
                        soc_pct_end=step.soc_pct_end,
                        marginal_value=step.marginal_value,
                    )
                )

        self._publish_recommendation(decision, status, objective)
        logger.info("Optimise run %s status=%s objective=%s", run_id, status.value, objective)
        return run_id

    def optimiser_params(self) -> OptimiserParams:
        s = self.settings
        return OptimiserParams(
            battery_capacity_kwh=s.battery_capacity_kwh,
            soc_min_kwh=s.soc_min_kwh,
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
            terminal_soc_salvage_pln_kwh=s.terminal_soc_salvage_pln_kwh,
        )

    # --- helpers -----------------------------------------------------------
    def _latest_soc_pct(self) -> float | None:
        with self.store.session() as session:
            row = session.execute(
                select(Telemetry).order_by(Telemetry.ts.desc()).limit(1)
            ).scalar_one_or_none()
            return row.soc_pct if row else None

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

    def _interval_grid(self, prices: list[Price]) -> list[tuple[dt.datetime, float]]:
        """The (interval_start, dt_hours) grid the optimiser runs on: hourly prices expanded
        into aligned sub-hour steps. Forecasts are computed on exactly this grid."""
        step_h = self.settings.step_hours
        substeps = max(1, int(round(1.0 / step_h)))
        grid: list[tuple[dt.datetime, float]] = []
        for p in sorted(prices, key=lambda x: x.interval_start):
            if p.buy_gross is None:
                continue
            hour_start = _aware(p.interval_start)
            for k in range(substeps):
                grid.append((hour_start + dt.timedelta(hours=step_h * k), step_h))
        return grid

    def _build_intervals(
        self,
        prices: list[Price],
        pv_map: dict[dt.datetime, float],
        load_map: dict[dt.datetime, float],
    ) -> list[IntervalInput]:
        """Expand hourly prices to aligned sub-hour steps; attach PV/load forecasts if present."""
        step_h = self.settings.step_hours
        substeps = max(1, int(round(1.0 / step_h)))
        intervals: list[IntervalInput] = []
        for p in sorted(prices, key=lambda x: x.interval_start):
            if p.buy_gross is None:
                continue
            hour_start = _aware(p.interval_start)
            for k in range(substeps):
                start = hour_start + dt.timedelta(hours=step_h * k)
                intervals.append(
                    IntervalInput(
                        interval_start=start.isoformat(),
                        dt_hours=step_h,
                        pv_energy_kwh=pv_map.get(start, 0.0),
                        load_energy_kwh=load_map.get(start, 0.0),
                        buy_price=float(p.buy_gross),
                        sell_price=float(p.sell_gross or 0.0),
                        price_is_real=(p.source == "api"),
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
        grid = self._interval_grid(prices)
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
        lookback = now - dt.timedelta(days=LOAD_LOOKBACK_DAYS)
        with self.store.session() as session:
            rows = session.execute(
                select(Telemetry.ts, Telemetry.load_kw)
                .where(Telemetry.ts >= lookback)
                .where(Telemetry.load_kw.is_not(None))
            ).all()
        return [LoadSample(ts=_aware(ts), load_kw=load_kw) for ts, load_kw in rows]

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
        self, decision, status: Status, objective: float | None  # noqa: ANN001
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


def _hourly_from_map(values: dict[dt.datetime, float]) -> dict[dt.datetime, float]:
    """Aggregate a sub-hour-step map into hourly sums for compact forecast persistence."""
    out: dict[dt.datetime, float] = {}
    for ts, value in values.items():
        hour = _aware(ts).replace(minute=0, second=0, microsecond=0)
        out[hour] = out.get(hour, 0.0) + value
    return out


def _hourly_mean_states(states: list[HaState]) -> dict[dt.datetime, float]:
    """Mean of numeric recorder states bucketed by UTC hour (mirrors routes aggregation)."""
    buckets: dict[dt.datetime, list[float]] = {}
    for st in states:
        if st.last_updated is None:
            continue
        value = st.as_float()
        if value is None:
            continue
        hour = _aware(st.last_updated).replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(value)
    return {hour: sum(vals) / len(vals) for hour, vals in buckets.items()}


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _is_iso(value: str) -> bool:
    try:
        dt.datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False
