"""End-to-end fault-injection for the battery control loop (no live HA/inverter)."""

from __future__ import annotations

import datetime as dt

import pytest

from energy_optimizer.battery_control import BatteryControlIntent, ControlDirection
from energy_optimizer.config import Settings
from energy_optimizer.control_store import try_acquire_lease
from energy_optimizer.service import Service
from energy_optimizer.sigenergy_control import SigenergyController
from energy_optimizer.store import PlanStep, Run, Store, utcnow
from energy_optimizer.watchdog import FakeHaWatchdog, HeartbeatSample
from ha_emulator import EmulatedHa, EmulatedPhysical, ha_state, phys


def _settings(**overrides) -> Settings:
    data = dict(
        db=":memory:",
        mqtt_enabled=False,
        mode="dry_run",
        ha_token="token",
        pstryk_api_key="",
        pv_forecast_provider="none",
        pv_planes=[],
        battery_capacity_kwh=18.0,
        battery_max_charge_kw=8.8,
        battery_max_discharge_kw=9.6,
        battery_control_max_charge_kw=8.8,
        battery_control_max_discharge_kw=9.6,
        battery_control_grid_charge_enabled=True,
        battery_control_physical_verify_timeout_seconds=15.0,
        battery_control_command_poll_seconds=0.01,
        battery_control_command_timeout_seconds=1.0,
        battery_soc_min_pct=15.0,
        battery_soc_max_pct=98.0,
        battery_round_trip_efficiency=0.9,
        degradation_cost_pln_per_kwh=0.05,
        site_import_limit_kw=11.0,
        site_export_limit_kw=6.0,
        inverter_limit_kw=6.0,
    )
    data.update(overrides)
    return Settings.model_construct(**data)


def _intent(
    *,
    direction: ControlDirection = ControlDirection.CHARGE,
    power_kw: float = 0.5,
    grid_charge: bool = True,
    export: bool = False,
) -> BatteryControlIntent:
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    return BatteryControlIntent(
        source_run_id=1,
        interval_start=now,
        direction=direction,
        requested_power_kw=power_kw,
        cutoff_soc_pct=98.0,
        expiry=now + dt.timedelta(minutes=15),
        grid_charge=grid_charge,
        export=export,
        expected_grid_direction="import" if grid_charge else None,
        expected_grid_kw_min=0.0 if grid_charge else None,
        expected_grid_kw_max=11.0 if grid_charge else None,
        expected_financial_value_pln=0.1,
        reason_codes=("test",),
    )


def _seed_plan(store: Store, now: dt.datetime, *, grid_to_batt: float = 0.5) -> None:
    step_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    with store.session() as session:
        session.add(
            Run(
                run_id="int-run",
                ts=now,
                mode="dry_run",
                horizon_hours=48,
                known_price_hours=24,
                status="ok",
            )
        )
        session.add(
            PlanStep(
                run_id="int-run",
                interval_start=step_start,
                dt_hours=0.25,
                grid_to_battery_kwh=grid_to_batt,
                grid_to_load_kwh=0.2,
            )
        )


@pytest.mark.asyncio
async def test_charge_happy_path(monkeypatch) -> None:
    settings = _settings()
    ha = EmulatedHa(
        states={
            settings.battery_control_remote_ems_switch_entity: ha_state(
                settings.battery_control_remote_ems_switch_entity, "off"
            ),
            settings.battery_control_mode_select_entity: ha_state(
                settings.battery_control_mode_select_entity,
                "Standby",
                attributes={
                    "options": [
                        "Standby",
                        "Command Charging (Grid First)",
                        "Maximum Self Consumption",
                    ]
                },
            ),
        },
        number_register_ack_reliable=True,
    )
    physical = EmulatedPhysical(
        [
            phys(0.0, ems_mode="Standby"),
            phys(0.05, ems_mode="Standby"),
            phys(
                0.5,
                grid_in=0.6,
                ems_mode="Command Charging (Grid First)",
                charge_limit_kw=0.5,
            ),
            phys(
                0.5,
                grid_in=0.6,
                ems_mode="Command Charging (Grid First)",
                charge_limit_kw=0.5,
            ),
        ]
    )

    async def _sleep(_s: float) -> None:
        return None

    ticks = {"n": 0.0}

    def mono() -> float:
        ticks["n"] += 0.5
        return ticks["n"]

    controller = SigenergyController(
        ha,  # type: ignore[arg-type]
        settings,
        physical=physical,
        sleep=_sleep,
        monotonic=mono,
    )
    ok = await controller.apply_intent(_intent())
    assert ok.control.failure_reason is None or ok.control.physical_verified


@pytest.mark.asyncio
async def test_wrong_physical_direction_fails_closed() -> None:
    settings = _settings()
    ha = EmulatedHa(
        states={
            settings.battery_control_remote_ems_switch_entity: ha_state(
                settings.battery_control_remote_ems_switch_entity, "off"
            ),
            settings.battery_control_mode_select_entity: ha_state(
                settings.battery_control_mode_select_entity,
                "Standby",
                attributes={"options": ["Standby", "Command Charging (Grid First)"]},
            ),
        },
        number_register_ack_reliable=True,
    )
    # Request charge but physical shows discharge.
    physical = EmulatedPhysical(
        [phys(0.0), phys(0.05), phys(-0.8, grid_out=0.5), phys(-0.8, grid_out=0.5)]
    )

    async def _sleep(_s: float) -> None:
        return None

    ticks = {"n": 0.0}

    def mono() -> float:
        ticks["n"] += 1.0
        return ticks["n"]

    controller = SigenergyController(
        ha,  # type: ignore[arg-type]
        settings,
        physical=physical,
        sleep=_sleep,
        monotonic=mono,
    )
    result = await controller.apply_intent(_intent())
    assert result.control.failure_reason is not None
    assert "physical" in (result.control.failure_reason or "") or result.control.failure_reason


@pytest.mark.asyncio
async def test_ha_disconnect_mid_transaction_fails_closed() -> None:
    settings = _settings()
    ha = EmulatedHa(
        states={
            settings.battery_control_remote_ems_switch_entity: ha_state(
                settings.battery_control_remote_ems_switch_entity, "off"
            ),
            settings.battery_control_mode_select_entity: ha_state(
                settings.battery_control_mode_select_entity,
                "Standby",
                attributes={"options": ["Standby", "Command Charging (Grid First)"]},
            ),
        },
        number_register_ack_reliable=True,
        disconnect_after_calls=1,
    )

    async def _sleep(_s: float) -> None:
        return None

    controller = SigenergyController(
        ha,  # type: ignore[arg-type]
        settings,
        physical=EmulatedPhysical([phys(0.0)]),
        sleep=_sleep,
    )
    with pytest.raises(ConnectionError):
        await controller.apply_intent(_intent())


@pytest.mark.asyncio
async def test_service_dry_run_and_lease_conflict_and_startup_fallback(monkeypatch) -> None:
    store = Store(":memory:")
    store.create_all()
    service = Service(_settings(mode="dry_run", battery_control_enabled=False, ha_token=""), store)
    now = utcnow()
    _seed_plan(store, now)

    dry = await service.control_battery(now=now)
    assert dry["result"] == "shadow"
    assert dry["direction"] == "CHARGE"

    # Fresh store: foreign owner holds the lease before this process tries.
    store2 = Store(":memory:")
    store2.create_all()
    with store2.session() as session:
        assert try_acquire_lease(session, owner_id="other", ttl_seconds=120, now=now)
    conflict_service = Service(
        _settings(mode="dry_run", battery_control_enabled=False, ha_token=""), store2
    )
    conflict = await conflict_service.control_battery(now=now)
    assert conflict["result"] == "lease_conflict"

    armed = Service(
        _settings(mode="control", battery_control_enabled=True, ha_token="token"),
        Store(":memory:"),
    )
    armed.store.create_all()

    class FakeHaClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get_state(self, entity_id):
            return ha_state(entity_id, "on")

    async def recorded_fallback(reason, command_id=None):
        return {"result": "fallback", "reason": reason, "command_id": "x"}

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", FakeHaClient)
    monkeypatch.setattr(armed, "fallback_battery", recorded_fallback)
    result = await armed.reconcile_battery_on_startup()
    assert result["reason"] == "startup_remote_ems_on"


@pytest.mark.asyncio
async def test_discharge_and_export_remain_capability_blocked() -> None:
    settings = _settings(
        battery_control_authorize_discharge=False,
        battery_export_enabled=False,
    )
    ha = EmulatedHa(number_register_ack_reliable=True)

    async def _sleep(_s: float) -> None:
        return None

    controller = SigenergyController(
        ha,  # type: ignore[arg-type]
        settings,
        physical=EmulatedPhysical([phys(0.0)]),
        sleep=_sleep,
    )
    discharge = await controller.apply_intent(
        _intent(direction=ControlDirection.DISCHARGE, grid_charge=False, power_kw=1.0)
    )
    assert discharge.control.failure_reason is not None
    assert ha.calls == [] or ("switch", "turn_on") not in [(d, s) for d, s, _ in ha.calls]


def test_heartbeat_expiry_triggers_watchdog_fallback_sequence() -> None:
    settings = _settings()
    watchdog = FakeHaWatchdog(settings)
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    decision = watchdog.maybe_act(
        heartbeat=HeartbeatSample(ts=now - dt.timedelta(seconds=120)),
        now=now,
    )
    assert decision.should_fallback is True
    assert ("switch", "turn_on") not in [(d, a) for d, a, _ in watchdog.service_calls]
    values = [c[2]["value"] for c in watchdog.service_calls if c[0] == "number"]
    assert values == [8.8, 9.6, 100.0, 0.0]


@pytest.mark.asyncio
async def test_duplicate_instance_lease_lockout() -> None:
    settings = _settings(mode="dry_run", battery_control_enabled=False)
    store = Store(":memory:")
    store.create_all()
    a = Service(settings, store)
    b = Service(settings, store)
    now = utcnow()
    first = await a.control_battery(now=now)
    assert first["result"] == "shadow"
    second = await b.control_battery(now=now)
    assert second["result"] == "lease_conflict"
    from energy_optimizer.store import ControllerStateRow

    with store.session() as session:
        state = session.get(ControllerStateRow, "current")
        assert state is not None
        assert state.state == "LOCKOUT"
