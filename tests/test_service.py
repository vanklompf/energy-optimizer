from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from energy_optimizer.config import Settings
from energy_optimizer.ev import EvRequirements
from energy_optimizer.ha_client import HaState
from energy_optimizer.safety import SafetyReport, Status
from energy_optimizer.service import (
    Service,
    _apply_ev_shortfall_warning,
    _ev_fault_status,
    _ev_live_state,
    _hourly_from_map,
    _hourly_mean_states,
)
from energy_optimizer.store import (
    EvControlStatus,
    EvPlanStep,
    EvTelemetry,
    PlanStep,
    Price,
    Run,
    Store,
    Telemetry,
    utcnow,
)


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
        steps = (
            session.execute(select(PlanStep).where(PlanStep.run_id == run_id)).scalars().all()
        )
    assert run is not None
    assert run.status != "blocked"
    assert len(steps) > 0


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


def test_hourly_mean_states_buckets_by_hour() -> None:
    hour = dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC)

    def state(minute: int, value: str) -> HaState:
        return HaState(
            entity_id="sensor.x",
            state=value,
            last_updated=hour + dt.timedelta(minutes=minute),
            attributes={},
        )

    means = _hourly_mean_states([state(0, "2.0"), state(30, "4.0"), state(45, "unknown")])
    assert means[hour] == 3.0


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


async def test_run_optimise_adds_shadow_vehicle_slots_while_relay_control_is_disabled() -> None:
    settings = _settings().model_copy(
        update={
            "ev_control_enabled": False,
            "ev_target_soc_pct": 100.0,
            "ev_minimum_target_soc_pct": 75.0,
            "ev_charge_power_kw": 1.8,
            "ev_capacity_kwh": 10.9,
            "ev_charge_efficiency": 0.9,
        }
    )
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
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
            session.execute(select(EvPlanStep).where(EvPlanStep.run_id == run_id))
            .scalars()
            .all()
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
        "expected_desired_on",
        "reason_fragment",
    ),
    [
        (["on"], False, False, "token", False, False, True, "selected current charging slot"),
        (["off", "off"], False, False, "token", False, False, False, "forced OFF confirmed"),
        (["off"], True, False, "token", False, False, False, "forced OFF confirmed"),
        (["off"], False, True, "token", False, False, False, "forced OFF confirmed"),
        ([], False, False, None, False, False, False, "credential unavailable"),
        ([], False, False, "token", True, False, False, "control channel unavailable"),
        (["off"], False, False, "token", False, True, False, "pipeline failure"),
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
    expected_desired_on,
    reason_fragment,
) -> None:
    settings = _settings().model_copy(
        update={"ha_token": ha_token, "ev_control_enabled": True}
    )
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
            return HaState(
                entity_id, verification_states.pop(0), now, {}, last_changed=now
            )

    nonlocal_on_readback_raises = [on_readback_raises]
    monkeypatch.setattr("energy_optimizer.service.HaClient", FakeHaClient)

    await service.control_ev_charging(now=now, force_off=force_off)

    expected_calls: list[tuple] = []
    if ha_token and not context_raises and force_off:
        expected_calls.extend(
            [
                ("switch", "turn_off", {"entity_id": settings.ev_switch_entity}),
                ("get_state", settings.ev_switch_entity),
            ]
        )
    elif ha_token and not context_raises:
        expected_calls.append(
            ("switch", "turn_on", {"entity_id": settings.ev_switch_entity})
        )
        if not turn_on_raises:
            expected_calls.append(("get_state", settings.ev_switch_entity))
        if not expected_desired_on:
            expected_calls.extend(
                [
                    ("switch", "turn_off", {"entity_id": settings.ev_switch_entity}),
                    ("get_state", settings.ev_switch_entity),
                ]
            )
    assert calls == expected_calls
    with store.session() as session:
        control = session.get(EvControlStatus, "current")
    assert control is not None
    assert control.desired_on is expected_desired_on
    assert control.action == ("turn_on" if expected_desired_on else "turn_off")
    assert reason_fragment in control.reason


async def test_control_ev_charging_one_shot_override_ignores_deferred_plan(monkeypatch) -> None:
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

    await service.control_ev_charging(now=now)

    assert calls == [
        ("switch", "turn_on", {"entity_id": settings.ev_switch_entity}),
        ("get_state", settings.ev_switch_entity),
    ]
    with store.session() as session:
        control = session.get(EvControlStatus, "current")
    assert control is not None
    assert control.desired_on is True
    assert "one-shot" in control.reason
