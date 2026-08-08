from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from energy_optimizer.config import Settings
from energy_optimizer.ev import EvRequirements
from energy_optimizer.ev_control import EvControlDecision
from energy_optimizer.ha_client import HaState
from energy_optimizer.pstryk_client import MeterFrame
from energy_optimizer.safety import SafetyReport, Status
from energy_optimizer.service import (
    Service,
    _apply_ev_relay_decision,
    _apply_ev_shortfall_warning,
    _ev_fault_status,
    _ev_live_state,
    _hourly_from_map,
    _regular_state_samples,
    _relay_failure_backoff_decision,
    _soc_pct_or_reserve,
)
from energy_optimizer.store import (
    EvControlStatus,
    EvPlanStep,
    EvTelemetry,
    PlanStep,
    Price,
    PstrykMeterInterval,
    Run,
    Store,
    Telemetry,
    utcnow,
)


async def _no_sleep(_seconds: float) -> None:
    return None


def test_zero_soc_is_not_replaced_by_the_operating_reserve() -> None:
    assert _soc_pct_or_reserve(0.0, 15.0) == 0.0
    assert _soc_pct_or_reserve(None, 15.0) == 15.0


def _settings() -> Settings:
    # pv_forecast_provider="none" keeps the optimiser path fully offline (no Forecast.Solar).
    return Settings(
        db=":memory:",
        mqtt_enabled=False,
        ha_token="",
        pstryk_api_key="",
        pv_forecast_provider="none",
        pv_planes=[],
        battery_capacity_kwh=10.0,
        ev_relay_settle_seconds=0,
        ev_relay_verify_interval_seconds=1,
        ev_relay_verify_timeout_seconds=2,
        battery_max_charge_kw=5.0,
        battery_max_discharge_kw=5.0,
        battery_soc_min_pct=20.0,
        battery_soc_max_pct=98.0,
        battery_round_trip_efficiency=0.90,
        degradation_cost_pln_per_kwh=0.05,
        site_import_limit_kw=14.0,
        site_export_limit_kw=14.0,
        inverter_limit_kw=12.0,
    )


def test_departure_shortfall_is_exposed_as_low_confidence_warning() -> None:
    report = SafetyReport(status=Status.OK)
    requirements = EvRequirements(
        departure_at=dt.datetime(2026, 8, 3, 7, 0, tzinfo=dt.UTC),
        minimum_slots=2,
        target_slots=2,
        minimum_shortfall_slots=7,
        target_shortfall_slots=7,
    )

    _apply_ev_shortfall_warning(report, requirements, step_minutes=15)

    assert report.status == Status.LOW_CONFIDENCE
    assert report.warnings == [
        "EV departure target infeasible by 7 slots (105 minutes); "
        "charging every available pre-departure slot"
    ]


def test_stale_ev_telemetry_makes_relay_state_unknown() -> None:
    now = utcnow()
    live = _ev_live_state(
        EvTelemetry(
            ts=now - dt.timedelta(minutes=11),
            soc_pct=40,
            charging_status="2",
            charging_active=False,
            switch_on=False,
            switch_changed=now - dt.timedelta(hours=1),
            power_kw=0,
            fault=False,
            stale=False,
        ),
        now,
    )

    assert live.switch_on is None
    assert live.switch_last_changed is None
    assert live.power_kw is None


async def test_run_optimise_produces_plan_from_load_forecast() -> None:
    """With fresh telemetry, a current-hour price and telemetry history for the load
    forecast, a run is no longer blocked and emits plan steps (regression for the
    nighttime-stale blocker + forecast wiring)."""
    settings = _settings()
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)

    now = utcnow()
    floor = now.replace(minute=0, second=0, microsecond=0)
    with store.session() as session:
        for h in range(6):
            buy = 0.5 if h < 3 else 2.0
            session.add(
                Price(
                    interval_start=floor + dt.timedelta(hours=h),
                    buy_gross=buy,
                    full_price=buy,
                    sell_gross=buy * 0.5,
                    source="api",
                )
            )
        # Two days of hourly telemetry so the load forecaster has samples; the most recent
        # sample is "now" (fresh) with PV pinned at 0 (nighttime steady state).
        for h in range(48):
            session.add(
                Telemetry(
                    ts=now - dt.timedelta(hours=h),
                    soc_pct=50.0,
                    pv_kw=0.0,
                    load_kw=1.0,
                    grid_import_kw=1.0,
                    grid_export_kw=0.0,
                    batt_charge_kw=0.0,
                    batt_discharge_kw=0.0,
                    stale=False,
                )
            )

    run_id = await service.run_optimise()

    with store.session() as session:
        run = session.get(Run, run_id)
        steps = session.execute(select(PlanStep).where(PlanStep.run_id == run_id)).scalars().all()
    assert run is not None
    assert run.status != "blocked"
    assert len(steps) > 0


async def test_refresh_meter_values_persists_authoritative_billing_intervals(monkeypatch) -> None:
    settings = _settings().model_copy(update={"pstryk_api_key": "secret"})
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    start = dt.datetime(2026, 8, 7, 7, 0, tzinfo=dt.UTC)

    class FakePstrykClient:
        calls = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def fetch_meter_values(self, window_start, window_end, resolution="hour"):
            assert resolution == "hour"
            type(self).calls += 1
            if type(self).calls == 3:
                return []
            imported = 0.053 if type(self).calls == 1 else 0.1
            return [
                MeterFrame(
                    interval_start=start,
                    interval_end=start + dt.timedelta(hours=1),
                    import_kwh=imported,
                    export_kwh=0.149,
                    balance_kwh=-0.096,
                )
            ]

    monkeypatch.setattr("energy_optimizer.service.PstrykClient", FakePstrykClient)

    assert await service.refresh_meter_values(days_back=7) == 1
    with store.session() as session:
        row = session.get(PstrykMeterInterval, start)
    assert row is not None
    assert row.import_kwh == 0.053
    assert row.export_kwh == 0.149
    assert row.balance_kwh == -0.096
    assert row.source == "pstryk"

    assert await service.refresh_meter_values(days_back=7) == 1
    with store.session() as session:
        rows = session.execute(select(PstrykMeterInterval)).scalars().all()
    assert len(rows) == 1
    assert rows[0].import_kwh == 0.1

    assert await service.refresh_meter_values(days_back=7) == 0
    with store.session() as session:
        rows = session.execute(select(PstrykMeterInterval)).scalars().all()
    assert rows == []


def test_pstryk_meter_reconciles_load_history_used_for_forecasting() -> None:
    settings = _settings()
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    hour = utcnow().replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    with store.session() as session:
        for minute in (0, 15, 30, 45):
            session.add(
                Telemetry(
                    ts=hour + dt.timedelta(minutes=minute),
                    soc_pct=50.0,
                    pv_kw=2.0,
                    load_kw=None,  # load derives from the Pstryk-settled energy balance
                    grid_import_kw=0.0,
                    grid_export_kw=0.0,
                    batt_charge_kw=0.4,
                    batt_discharge_kw=0.5,
                    stale=False,
                )
            )
        session.add(
            PstrykMeterInterval(
                interval_start=hour,
                interval_end=hour + dt.timedelta(hours=1),
                import_kwh=0.1,
                export_kwh=0.2,
                balance_kwh=-0.1,
                source="pstryk",
            )
        )

    samples = service._load_samples(utcnow())

    assert len(samples) == 1
    # load = PV + import + battery discharge - export - battery charge
    assert samples[0].load_kw == pytest.approx(2.0)


def test_load_history_has_no_sigen_fallback_without_pstryk_meter() -> None:
    settings = _settings()
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    hour = utcnow().replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    with store.session() as session:
        session.add(
            Telemetry(
                ts=hour,
                soc_pct=50.0,
                pv_kw=0.0,
                load_kw=None,
                grid_import_kw=9.0,
                grid_export_kw=0.0,
                batt_charge_kw=0.0,
                batt_discharge_kw=0.0,
                stale=False,
            )
        )

    assert service._load_samples(utcnow()) == []


def test_hourly_from_map_sums_substeps_into_hours() -> None:
    base = dt.datetime(2026, 7, 13, 10, 0, tzinfo=dt.UTC)
    values = {
        base: 0.25,
        base + dt.timedelta(minutes=15): 0.25,
        base + dt.timedelta(minutes=30): 0.25,
        base + dt.timedelta(minutes=45): 0.25,
        base + dt.timedelta(hours=1): 1.0,
    }
    hourly = _hourly_from_map(values)
    assert hourly[base] == 1.0
    assert hourly[base + dt.timedelta(hours=1)] == 1.0


def test_regular_state_samples_holds_recorder_values_at_five_minute_steps() -> None:
    hour = dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC)

    def state(minute: int, value: str) -> HaState:
        return HaState(
            entity_id="sensor.x",
            state=value,
            last_updated=hour + dt.timedelta(minutes=minute),
            attributes={},
        )

    samples = _regular_state_samples(
        [state(0, "2.0"), state(30, "4.0"), state(45, "unknown")],
        hour,
        hour + dt.timedelta(hours=1),
    )
    assert len(samples) == 9
    assert samples[hour + dt.timedelta(minutes=25)] == 2.0
    assert samples[hour + dt.timedelta(minutes=40)] == 4.0
    assert hour + dt.timedelta(minutes=45) not in samples


def test_ev_fault_status_fails_closed_for_unavailable_protection_sensor() -> None:
    now = utcnow()
    off = HaState("binary_sensor.guard", "off", now, {})
    unavailable = HaState("binary_sensor.guard", "unavailable", now, {})

    assert _ev_fault_status([off, off]) == (False, False)
    assert _ev_fault_status([off, unavailable]) == (True, True)
    assert _ev_fault_status([off, None]) == (True, True)


async def test_collect_ev_telemetry_persists_vehicle_and_charger_state(monkeypatch) -> None:
    settings = _settings().model_copy(
        update={
            "ha_token": "token",
            "ev_control_enabled": True,
        }
    )
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    now = utcnow()

    class FakeHaClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def get_states(self, entity_ids):
            values = {
                settings.ev_soc_entity: "62",
                settings.ev_charging_status_entity: "2",
                settings.ev_charging_active_entity: "off",
                settings.ev_charge_to_100_entity: "on",
                settings.ev_switch_entity: "on",
                settings.ev_power_entity: "1800",
                **{entity_id: "off" for entity_id in settings.ev_fault_entities},
            }
            return {
                entity_id: HaState(entity_id, values[entity_id], now, {})
                for entity_id in entity_ids
            }

    monkeypatch.setattr("energy_optimizer.service.HaClient", FakeHaClient)

    await service.collect_ev_telemetry()

    with store.session() as session:
        row = session.execute(select(EvTelemetry).order_by(EvTelemetry.ts.desc())).scalar_one()
    assert row.soc_pct == 62.0
    assert row.charging_status == "2"
    assert row.switch_on is True
    assert row.power_kw == 1.8
    assert row.fault is False
    assert service.ev_charge_to_100_active is True


async def test_run_optimise_adds_shadow_vehicle_slots_while_relay_control_is_disabled(
    monkeypatch,
) -> None:
    settings = _settings().model_copy(
        update={
            "ev_control_enabled": False,
            "ev_minimum_target_soc_pct": 100.0,
            "ev_charge_power_kw": 1.8,
            "ev_capacity_kwh": 10.9,
            "ev_charge_efficiency": 0.9,
        }
    )
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)

    async def forecast_with_surplus(forecast_now, forecast_prices):
        starts = [start for start, _ in service._interval_grid(forecast_prices, forecast_now)]
        return (
            {start: 1.0 for start in starts},
            {start: 0.1 for start in starts},
            "ok",
            "ok",
        )

    monkeypatch.setattr(service, "_forecast_maps_live", forecast_with_surplus)
    now = utcnow()
    floor = now.replace(minute=0, second=0, microsecond=0)
    with store.session() as session:
        for h in range(8):
            session.add(
                Price(
                    interval_start=floor + dt.timedelta(hours=h),
                    buy_gross=0.5 if h >= 2 else 2.0,
                    full_price=0.5 if h >= 2 else 2.0,
                    sell_gross=0.2,
                    source="api",
                )
            )
        for h in range(48):
            session.add(
                Telemetry(
                    ts=now - dt.timedelta(hours=h),
                    soc_pct=50.0,
                    pv_kw=0.0,
                    load_kw=1.0,
                    grid_import_kw=1.0,
                    grid_export_kw=0.0,
                    batt_charge_kw=0.0,
                    batt_discharge_kw=0.0,
                    stale=False,
                )
            )
        session.add(
            EvTelemetry(
                ts=now,
                soc_pct=90.0,
                charging_status="2",
                charging_active=False,
                switch_on=False,
                switch_changed=now - dt.timedelta(minutes=30),
                power_kw=0.0,
                fault=False,
                stale=False,
            )
        )

    run_id = await service.run_optimise()

    with store.session() as session:
        ev_steps = (
            session.execute(select(EvPlanStep).where(EvPlanStep.run_id == run_id)).scalars().all()
        )
    assert sum(step.charge_kwh for step in ev_steps) >= 10.9 * 0.10 / 0.9
    assert any(step.planned_on for step in ev_steps)


@pytest.mark.parametrize(
    (
        "verification_states",
        "turn_on_raises",
        "on_readback_raises",
        "ha_token",
        "context_raises",
        "force_off",
        "expected_readbacks",
        "expected_desired_on",
        "reason_fragment",
    ),
    [
        (["on"], False, False, "token", False, False, 1, True, "selected current charging slot"),
        (
            ["off", "on"],
            False,
            False,
            "token",
            False,
            False,
            2,
            True,
            "selected current charging slot",
        ),
        (
            ["off", "off", "off", "off"],
            False,
            False,
            "token",
            False,
            False,
            4,
            False,
            "forced OFF confirmed",
        ),
        (["off"], True, False, "token", False, False, 1, False, "forced OFF confirmed"),
        (
            ["off", "off", "off"],
            False,
            True,
            "token",
            False,
            False,
            4,
            False,
            "forced OFF confirmed",
        ),
        ([], False, False, None, False, False, 0, False, "credential unavailable"),
        ([], False, False, "token", True, False, 0, False, "control channel unavailable"),
        (["off"], False, False, "token", False, True, 1, False, "pipeline failure"),
    ],
)
async def test_control_ev_charging_verifies_shelly_actuation_and_fails_safe(
    monkeypatch,
    verification_states,
    turn_on_raises,
    on_readback_raises,
    ha_token,
    context_raises,
    force_off,
    expected_readbacks,
    expected_desired_on,
    reason_fragment,
) -> None:
    settings = _settings().model_copy(update={"ha_token": ha_token, "ev_control_enabled": True})
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    now = utcnow()
    step_start = now.replace(
        minute=(now.minute // settings.step_minutes) * settings.step_minutes,
        second=0,
        microsecond=0,
    )
    with store.session() as session:
        session.add(
            Run(
                run_id="ev-run",
                ts=now,
                mode="dry_run",
                horizon_hours=48,
                known_price_hours=24,
                status="ok",
            )
        )
        session.add(
            EvPlanStep(
                run_id="ev-run",
                interval_start=step_start,
                charge_kwh=settings.ev_charge_power_kw * settings.step_hours,
                planned_on=True,
            )
        )
        session.add(
            EvTelemetry(
                ts=now,
                soc_pct=40.0,
                charging_status="2",
                charging_active=False,
                switch_on=False,
                switch_changed=now - dt.timedelta(minutes=30),
                power_kw=0.0,
                fault=False,
                stale=False,
            )
        )

    calls: list[tuple] = []

    class FakeHaClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            if context_raises:
                raise RuntimeError("HA client unavailable")
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def call_service(self, domain, action, data) -> None:
            calls.append((domain, action, data))
            if action == "turn_on" and turn_on_raises:
                raise RuntimeError("ambiguous turn_on timeout")

        async def get_state(self, entity_id):
            calls.append(("get_state", entity_id))
            if nonlocal_on_readback_raises[0]:
                nonlocal_on_readback_raises[0] = False
                raise RuntimeError("ambiguous readback timeout")
            return HaState(entity_id, verification_states.pop(0), now, {}, last_changed=now)

    nonlocal_on_readback_raises = [on_readback_raises]
    monkeypatch.setattr("energy_optimizer.service.HaClient", FakeHaClient)
    monkeypatch.setattr("energy_optimizer.service.asyncio.sleep", _no_sleep)

    await service.control_ev_charging(now=now, force_off=force_off)

    expected_calls: list[tuple] = []
    if ha_token and not context_raises and force_off:
        expected_calls.append(("switch", "turn_off", {"entity_id": settings.ev_switch_entity}))
    elif ha_token and not context_raises:
        expected_calls.append(("switch", "turn_on", {"entity_id": settings.ev_switch_entity}))
        normal_readbacks = 0 if turn_on_raises else expected_readbacks
        if not expected_desired_on:
            normal_readbacks = max(0, normal_readbacks - 1)
        expected_calls.extend([("get_state", settings.ev_switch_entity)] * normal_readbacks)
        if not expected_desired_on:
            expected_calls.append(("switch", "turn_off", {"entity_id": settings.ev_switch_entity}))
    expected_calls.extend(
        [("get_state", settings.ev_switch_entity)]
        * (expected_readbacks - sum(call[0] == "get_state" for call in expected_calls))
    )
    assert calls == expected_calls
    with store.session() as session:
        control = session.get(EvControlStatus, "current")
    assert control is not None
    assert control.desired_on is expected_desired_on
    assert control.action == ("turn_on" if expected_desired_on else "turn_off")
    assert reason_fragment in control.reason


async def test_control_ev_charging_immediate_override_ignores_deferred_plan(monkeypatch) -> None:
    settings = _settings().model_copy(update={"ha_token": "token", "ev_control_enabled": True})
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    service.ev_charge_to_100_active = True
    now = utcnow()
    step_start = now.replace(
        minute=(now.minute // settings.step_minutes) * settings.step_minutes,
        second=0,
        microsecond=0,
    )
    with store.session() as session:
        session.add(
            Run(
                run_id="deferred-run",
                ts=now,
                mode="dry_run",
                horizon_hours=48,
                known_price_hours=24,
                status="ok",
            )
        )
        session.add(
            EvPlanStep(
                run_id="deferred-run",
                interval_start=step_start,
                charge_kwh=0.0,
                planned_on=False,
            )
        )
        session.add(
            EvTelemetry(
                ts=now,
                soc_pct=83.0,
                charging_status="2",
                charging_active=False,
                switch_on=False,
                switch_changed=now - dt.timedelta(minutes=30),
                power_kw=0.0,
                fault=False,
                stale=False,
            )
        )

    calls: list[tuple] = []

    class FakeHaClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def call_service(self, domain, action, data) -> None:
            calls.append((domain, action, data))

        async def get_state(self, entity_id):
            calls.append(("get_state", entity_id))
            return HaState(entity_id, "on", now, {}, last_changed=now)

    monkeypatch.setattr("energy_optimizer.service.HaClient", FakeHaClient)
    monkeypatch.setattr("energy_optimizer.service.asyncio.sleep", _no_sleep)

    await service.control_ev_charging(now=now)

    assert calls == [
        ("switch", "turn_on", {"entity_id": settings.ev_switch_entity}),
        ("get_state", settings.ev_switch_entity),
    ]
    with store.session() as session:
        control = session.get(EvControlStatus, "current")
    assert control is not None
    assert control.desired_on is True
    assert "immediate charging override" in control.reason


async def test_relay_waits_for_settle_period_then_polls_until_confirmation(monkeypatch) -> None:
    now = utcnow()
    sleeps: list[float] = []
    calls: list[tuple] = []
    states = ["off", "on"]

    class FakeHaClient:
        async def call_service(self, domain, action, data) -> None:
            calls.append((domain, action, data))

        async def get_state(self, entity_id):
            calls.append(("get_state", entity_id))
            return HaState(entity_id, states.pop(0), now, {}, last_changed=now)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("energy_optimizer.service.asyncio.sleep", fake_sleep)
    decision = EvControlDecision(True, "turn_on", "forecast-backed opportunistic charging")

    result = await _apply_ev_relay_decision(
        FakeHaClient(),
        "switch.garage",
        decision,
        settle_seconds=5.0,
        verify_interval_seconds=2.0,
        verify_timeout_seconds=30.0,
    )

    assert result == decision
    assert sleeps == [5.0, 2.0]
    assert calls == [
        ("switch", "turn_on", {"entity_id": "switch.garage"}),
        ("get_state", "switch.garage"),
        ("get_state", "switch.garage"),
    ]


def test_confirmed_fallback_off_suppresses_relay_retry_during_backoff() -> None:
    now = utcnow()
    previous = EvControlStatus(
        key="current",
        ts=now - dt.timedelta(minutes=10),
        desired_on=False,
        planned_on=True,
        action="turn_off",
        reason="CRITICAL: turn_on verification failed; forced OFF confirmed",
    )
    candidate = EvControlDecision(True, "turn_on", "optimiser selected current charging slot")

    decision = _relay_failure_backoff_decision(candidate, previous, now, 30)

    assert decision.desired_on is False
    assert decision.action == "none"
    assert "relay retry backoff" in decision.reason
    assert "20 minutes" in decision.reason


def test_unconfirmed_fallback_keeps_requesting_fail_safe_off_during_backoff() -> None:
    now = utcnow()
    previous = EvControlStatus(
        key="current",
        ts=now - dt.timedelta(minutes=10),
        desired_on=False,
        planned_on=True,
        action="turn_off",
        reason="CRITICAL: turn_on verification failed; forced OFF could not be confirmed",
    )
    candidate = EvControlDecision(True, "turn_on", "optimiser selected current charging slot")

    decision = _relay_failure_backoff_decision(candidate, previous, now, 30)

    assert decision.desired_on is False
    assert decision.action == "turn_off"
    assert "physical OFF remains unconfirmed" in decision.reason
