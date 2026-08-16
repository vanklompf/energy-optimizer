from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

from energy_optimizer.config import Settings
from energy_optimizer.control_store import try_acquire_lease
from energy_optimizer.ha_client import HaError, HaState
from energy_optimizer.service import Service
from energy_optimizer.store import (
    ControlAction,
    ControllerStateRow,
    PlanStep,
    Run,
    Store,
    utcnow,
)


def _settings(**overrides) -> Settings:
    base = dict(
        db=":memory:",
        mqtt_enabled=False,
        ha_token="",
        pstryk_api_key="",
        pv_forecast_provider="none",
        pv_planes=[],
        battery_capacity_kwh=10.0,
        battery_max_charge_kw=8.8,
        battery_max_discharge_kw=9.6,
        battery_soc_min_pct=20.0,
        battery_soc_max_pct=98.0,
        battery_round_trip_efficiency=0.90,
        degradation_cost_pln_per_kwh=0.05,
        site_import_limit_kw=14.0,
        site_export_limit_kw=14.0,
        inverter_limit_kw=12.0,
        battery_control_max_charge_kw=8.8,
        battery_control_max_discharge_kw=9.6,
    )
    base.update(overrides)
    return Settings(**base)


async def test_control_battery_shadow_records_real_intent_without_ha(monkeypatch) -> None:
    settings = _settings()
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    interval = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    with store.session() as session:
        session.add(
            Run(
                run_id="shadow-run",
                ts=now,
                mode="dry_run",
                horizon_hours=48,
                known_price_hours=24,
                status="ok",
            )
        )
        session.add(
            PlanStep(
                run_id="shadow-run",
                interval_start=interval,
                dt_hours=0.25,
                pv_to_battery_kwh=1.0,
                grid_to_load_kwh=0.5,
            )
        )

    ha_calls: list[str] = []

    class BoomHa:
        def __init__(self, *args, **kwargs) -> None:
            ha_calls.append("init")

    monkeypatch.setattr("energy_optimizer.service.HaClient", BoomHa)

    result = await service.control_battery(now=now)
    assert result["result"] == "shadow"
    assert result["direction"] == "CHARGE"
    assert result["source_run_id"] == "shadow-run"
    assert ha_calls == []
    with store.session() as session:
        action = session.get(ControlAction, result["command_id"])
        assert action is not None
        assert action.result == "shadow"
        assert action.source_run_id == "shadow-run"
        assert action.requested_state == "CHARGE"
        assert action.observed_state == "DISARMED"
        intent = json.loads(action.intent_json or "{}")
        physical = json.loads(action.physical_json or "{}")
        assert intent["shadow"] is True
        assert "evidence" in intent
        assert physical["ha_writes"] == 0


async def test_control_battery_shadow_without_plan_still_skips_ha(monkeypatch) -> None:
    settings = _settings()
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)

    ha_calls: list[str] = []

    class BoomHa:
        def __init__(self, *args, **kwargs) -> None:
            ha_calls.append("init")

    monkeypatch.setattr("energy_optimizer.service.HaClient", BoomHa)

    result = await service.control_battery()
    assert result["result"] == "shadow"
    assert ha_calls == []
    with store.session() as session:
        action = session.get(ControlAction, result["command_id"])
        assert action is not None
        assert action.result == "shadow"
        assert action.authorization_allowed is False
        blockers = json.loads(action.blockers_json or "[]")
        assert any("stale_plan" in b for b in blockers)


async def test_control_battery_stale_plan_falls_back_without_ha(monkeypatch) -> None:
    # Bypass startup validators so we can exercise the armed control path safely offline.
    data = _settings().model_dump()
    data.update(
        mode="control",
        battery_control_enabled=True,
        battery_control_number_register_ack_reliable=True,
        ha_token="token",
    )
    settings = Settings.model_construct(**data)
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)

    class FakeHa:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

    class FakeController:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def fallback(self, reason, command_id=None):
            from energy_optimizer.battery_control import ControllerState, ControlResult
            from energy_optimizer.sigenergy_control import SigenergyControlResult

            return SigenergyControlResult(
                control=ControlResult(
                    command_id=command_id or "x",
                    requested_state=ControllerState.FALLBACK,
                    observed_state=ControllerState.DISARMED,
                    entity_readback={},
                    physical_verified=True,
                    retries=0,
                    latency_ms=1.0,
                    failure_reason=None,
                    lockout_reason=None,
                )
            )

    monkeypatch.setattr("energy_optimizer.service.HaClient", FakeHa)
    monkeypatch.setattr(
        "energy_optimizer.sigenergy_control.SigenergyController", FakeController
    )

    result = await service.control_battery()
    assert result["result"] == "fallback"
    assert "stale_plan" in str(result.get("reason", ""))


async def test_control_battery_selects_containing_interval() -> None:
    settings = _settings(step_minutes=15)
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    early = dt.datetime(2026, 8, 8, 11, 45, tzinfo=dt.UTC)
    current = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    later = dt.datetime(2026, 8, 8, 12, 15, tzinfo=dt.UTC)
    with store.session() as session:
        session.add(
            Run(
                run_id="run-interval",
                ts=now,
                mode="dry_run",
                horizon_hours=48,
                known_price_hours=24,
                status="ok",
            )
        )
        for start, pv_to_batt in ((early, 0.0), (current, 1.0), (later, 0.0)):
            session.add(
                PlanStep(
                    run_id="run-interval",
                    interval_start=start,
                    dt_hours=0.25,
                    pv_to_battery_kwh=pv_to_batt,
                    grid_to_load_kwh=0.5,
                )
            )

    intent, run_id, interval_start, plan_age = service.build_battery_control_intent(now)
    assert run_id == "run-interval"
    assert interval_start == current
    assert intent.direction.value == "CHARGE"
    assert plan_age == 0.0


async def test_control_battery_lease_conflict_locks_out() -> None:
    settings = _settings()
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    now = utcnow()
    with store.session() as session:
        assert try_acquire_lease(session, owner_id="other-owner", ttl_seconds=120, now=now)

    result = await service.control_battery(now=now)
    assert result["result"] == "lease_conflict"
    with store.session() as session:
        state = session.get(ControllerStateRow, "current")
        assert state is not None
        assert state.state == "LOCKOUT"
        assert state.lockout_reason == "lease_conflict"


async def test_fallback_battery_records_when_disarmed() -> None:
    settings = _settings()
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    result = await service.fallback_battery("unit_test")
    assert result["result"] == "fallback_recorded"
    with store.session() as session:
        action = session.get(ControlAction, result["command_id"])
        assert action is not None
        assert action.result == "fallback_recorded"
        state = session.get(ControllerStateRow, "current")
        assert state is not None
        assert state.last_fallback_at is not None


async def test_reconcile_startup_falls_back_when_remote_ems_on(monkeypatch) -> None:
    data = _settings().model_dump()
    data.update(mode="control", battery_control_enabled=True, ha_token="token")
    settings = Settings.model_construct(**data)
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    with store.session() as session:
        session.add(
            ControlAction(
                command_id="pending-1",
                source_run_id=None,
                interval_start=None,
                intent_json="{}",
                authorization_allowed=True,
                blockers_json="[]",
                requested_state="ACTIVE_CHARGE",
                result="pending",
            )
        )

    class FakeHa:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get_state(self, entity_id):
            return HaState(entity_id, "on", utcnow(), {})

    monkeypatch.setattr("energy_optimizer.service.HaClient", FakeHa)

    async def _recorded_fallback(reason, command_id=None):
        return {"result": "fallback_recorded", "reason": reason, "command_id": "fb"}

    monkeypatch.setattr(service, "fallback_battery", _recorded_fallback)

    result = await service.reconcile_battery_on_startup()
    assert result["result"] == "fallback_recorded"
    assert result["reason"] == "startup_remote_ems_on"
    with store.session() as session:
        pending = session.get(ControlAction, "pending-1")
        assert pending is not None
        assert pending.result == "abandoned_on_restart"


async def test_reconcile_startup_falls_back_when_ha_read_fails(monkeypatch) -> None:
    """HA reachability loss at restart is unsafe until local EMS is explicitly restored."""
    data = _settings().model_dump()
    data.update(mode="control", battery_control_enabled=True, ha_token="token")
    settings = Settings.model_construct(**data)
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)

    class FailingHa:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get_state(self, entity_id: str):
            raise HaError(f"cannot read {entity_id}")

    monkeypatch.setattr("energy_optimizer.service.HaClient", FailingHa)

    async def fallback(reason: str, command_id=None):
        return {"result": "fallback_recorded", "reason": reason, "command_id": command_id}

    monkeypatch.setattr(service, "fallback_battery", fallback)

    result = await service.reconcile_battery_on_startup()

    assert result["result"] == "fallback_recorded"
    assert result["reason"] == "startup_remote_ems_unknown"


async def test_fallback_battery_records_lockout_when_ha_is_unreachable(monkeypatch) -> None:
    """An unavailable HA must leave auditable, unverified fallback/lockout evidence."""
    data = _settings().model_dump()
    data.update(mode="control", battery_control_enabled=True, ha_token="token")
    settings = Settings.model_construct(**data)
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)

    class FailingHa:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            raise HaError("HA unreachable")

        async def __aexit__(self, *args) -> None:
            return None

    monkeypatch.setattr("energy_optimizer.service.HaClient", FailingHa)

    result = await service.fallback_battery("watchdog_ha_error")

    assert result["result"] == "fallback_unreachable"
    with store.session() as session:
        action = session.get(ControlAction, result["command_id"])
        state = session.get(ControllerStateRow, "current")
        assert action is not None
        assert action.result == "fallback_unreachable"
        assert action.error_code == "watchdog_ha_error"
        assert state is not None
        assert state.state == "LOCKOUT"
        assert state.last_fallback_verified is False


async def test_path_loss_reconciliation_retries_safe_fallback_once_when_ha_returns(
    monkeypatch,
) -> None:
    """A path-loss lockout must request one verified safe restore when HA returns."""
    data = _settings().model_dump()
    data.update(
        mode="control",
        battery_control_enabled=True,
        battery_control_arm_token="pvopti-battery-control-armed",
        ha_token="token",
    )
    settings = Settings.model_construct(**data)
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)

    with store.session() as session:
        state = ControllerStateRow(
            key="current",
            state="LOCKOUT",
            lockout_reason="fallback_ha_unreachable",
            lockout_until=utcnow() + dt.timedelta(hours=1),
        )
        session.add(state)

    calls: list[str] = []

    async def fallback(reason: str, command_id=None):
        calls.append(reason)
        with store.session() as session:
            state = session.get(ControllerStateRow, "current")
            assert state is not None
            state.last_fallback_verified = True
        return {
            "result": "fallback",
            "reason": reason,
            "command_id": command_id,
            "verified": True,
        }

    monkeypatch.setattr(service, "fallback_battery", fallback)

    result = await service.reconcile_path_loss_recovery()
    repeated = await service.reconcile_path_loss_recovery()

    assert result["result"] == "reconnect_restore_verified"
    assert repeated["result"] == "reconnect_reconcile_not_needed"
    assert calls == ["reconnect_after_path_loss"]
    with store.session() as session:
        state = session.get(ControllerStateRow, "current")
        assert state is not None
        assert state.state == "LOCKOUT"
        assert state.lockout_reason == "path_recovered_restore_verified"
        assert state.last_fallback_verified is True


async def test_expired_path_loss_recovery_uses_only_off_local_fallback_actions(
    monkeypatch,
) -> None:
    """Reconnect recovery must not let an expired lockout resume the current plan."""
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    data = _settings().model_dump()
    data.update(
        mode="control",
        battery_control_enabled=True,
        battery_control_arm_token="pvopti-battery-control-armed",
        ha_token="token",
    )
    settings = Settings.model_construct(**data)
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    with store.session() as session:
        session.add(
            Run(
                run_id="would-actuate-if-unlocked",
                ts=now,
                mode="control",
                horizon_hours=48,
                known_price_hours=24,
                status="ok",
            )
        )
        session.add(
            PlanStep(
                run_id="would-actuate-if-unlocked",
                interval_start=now.replace(minute=0),
                dt_hours=0.25,
                grid_to_battery_kwh=0.5,
                grid_to_load_kwh=0.5,
            )
        )
        session.add(
            ControllerStateRow(
                key="current",
                state="LOCKOUT",
                lockout_reason="fallback_ha_unreachable",
                # Deliberately expired: path-loss reasons must remain latched.
                lockout_until=now - dt.timedelta(seconds=1),
            )
        )

    actuator_calls: list[tuple[str, str, dict]] = []
    controller_methods: list[str] = []

    class FakeHa:
        number_register_ack_reliable = True

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def select_option(self, entity_id: str, option: str, **kwargs) -> object:
            actuator_calls.append(
                ("select", "select_option", {"entity_id": entity_id, "option": option})
            )
            return object()

        async def set_number(self, entity_id: str, value: float, **kwargs) -> object:
            actuator_calls.append(("number", "set_value", {"entity_id": entity_id, "value": value}))
            return object()

        async def turn_switch(self, entity_id: str, on: bool, **kwargs) -> object:
            actuator_calls.append(
                ("switch", "turn_on" if on else "turn_off", {"entity_id": entity_id})
            )
            return object()

    class FallbackOnlyController:
        def __init__(self, ha, settings) -> None:
            self.ha = ha
            self.settings = settings

        async def apply_intent(self, intent):
            controller_methods.append("apply_intent")
            raise AssertionError("normal plan actuation must be unreachable during recovery")

        async def fallback(self, reason: str, *, command_id=None):
            controller_methods.append("fallback")
            await self.ha.select_option(
                self.settings.battery_control_mode_select_entity,
                self.settings.battery_control_fallback_mode,
            )
            for entity_id, value in (
                (
                    self.settings.battery_control_charge_limit_entity,
                    self.settings.battery_control_local_charge_limit_kw,
                ),
                (
                    self.settings.battery_control_discharge_limit_entity,
                    self.settings.battery_control_local_discharge_limit_kw,
                ),
                (
                    self.settings.battery_control_charge_cutoff_entity,
                    self.settings.battery_control_local_charge_cutoff_pct,
                ),
                (
                    self.settings.battery_control_discharge_cutoff_entity,
                    self.settings.battery_control_local_discharge_cutoff_pct,
                ),
            ):
                await self.ha.set_number(entity_id, value)
            await self.ha.turn_switch(self.settings.battery_control_remote_ems_switch_entity, False)
            return SimpleNamespace(
                control=SimpleNamespace(
                    observed_state=SimpleNamespace(value="DISARMED"),
                    physical_verified=True,
                    latency_ms=1.0,
                )
            )

    monkeypatch.setattr("energy_optimizer.service.HaClient", FakeHa)
    monkeypatch.setattr(
        "energy_optimizer.sigenergy_control.SigenergyController", FallbackOnlyController
    )

    recovered = await service.control_battery(now=now)
    later = await service.control_battery(now=now + dt.timedelta(days=1))

    assert recovered["result"] == "reconnect_restore_verified"
    assert later["result"] == "lockout"
    assert controller_methods == ["fallback"]
    assert actuator_calls == [
        (
            "select",
            "select_option",
            {
                "entity_id": settings.battery_control_mode_select_entity,
                "option": settings.battery_control_fallback_mode,
            },
        ),
        (
            "number",
            "set_value",
            {
                "entity_id": settings.battery_control_charge_limit_entity,
                "value": settings.battery_control_local_charge_limit_kw,
            },
        ),
        (
            "number",
            "set_value",
            {
                "entity_id": settings.battery_control_discharge_limit_entity,
                "value": settings.battery_control_local_discharge_limit_kw,
            },
        ),
        (
            "number",
            "set_value",
            {
                "entity_id": settings.battery_control_charge_cutoff_entity,
                "value": settings.battery_control_local_charge_cutoff_pct,
            },
        ),
        (
            "number",
            "set_value",
            {
                "entity_id": settings.battery_control_discharge_cutoff_entity,
                "value": settings.battery_control_local_discharge_cutoff_pct,
            },
        ),
        (
            "switch",
            "turn_off",
            {"entity_id": settings.battery_control_remote_ems_switch_entity},
        ),
    ]
    assert not any(action == "turn_on" for _, action, _ in actuator_calls)
    with store.session() as session:
        state = session.get(ControllerStateRow, "current")
        assert state is not None
        assert state.lockout_reason == "path_recovered_restore_verified"


async def test_unverified_path_loss_recovery_is_attempted_once_and_stays_locked(
    monkeypatch,
) -> None:
    """An unverified reconnect fallback never clears the path-loss lockout."""
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    data = _settings().model_dump()
    data.update(
        mode="control",
        battery_control_enabled=True,
        battery_control_arm_token="pvopti-battery-control-armed",
        ha_token="token",
    )
    settings = Settings.model_construct(**data)
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    with store.session() as session:
        session.add(
            ControllerStateRow(
                key="current",
                state="LOCKOUT",
                lockout_reason="fallback_ha_unreachable",
                lockout_until=now - dt.timedelta(seconds=1),
            )
        )

    fallback_attempts: list[str] = []

    async def unverified_fallback(reason: str, *, command_id=None):
        fallback_attempts.append(reason)
        return {"result": "fallback", "reason": reason, "verified": False}

    monkeypatch.setattr(service, "fallback_battery", unverified_fallback)

    first = await service.control_battery(now=now)
    repeated = await service.control_battery(now=now + dt.timedelta(days=1))

    assert first["result"] == "reconnect_restore_unverified"
    assert repeated["result"] == "lockout"
    assert fallback_attempts == ["reconnect_after_path_loss"]
    with store.session() as session:
        state = session.get(ControllerStateRow, "current")
        assert state is not None
        assert state.state == "LOCKOUT"
        assert state.lockout_reason == "path_loss_recovery_attempted_unverified"


async def test_unreachable_path_loss_recovery_is_attempted_once_and_stays_locked(
    monkeypatch,
) -> None:
    """A failed reconnect cannot retry indefinitely after its original timeout expires."""
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    data = _settings().model_dump()
    data.update(
        mode="control",
        battery_control_enabled=True,
        battery_control_arm_token="pvopti-battery-control-armed",
        ha_token="token",
    )
    settings = Settings.model_construct(**data)
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    with store.session() as session:
        session.add(
            ControllerStateRow(
                key="current",
                state="LOCKOUT",
                lockout_reason="fallback_ha_unreachable",
                lockout_until=now - dt.timedelta(seconds=1),
            )
        )

    connection_attempts: list[str] = []

    class UnreachableHa:
        def __init__(self, *args, **kwargs) -> None:
            connection_attempts.append("constructed")

        async def __aenter__(self):
            raise HaError("still unreachable")

        async def __aexit__(self, *args) -> None:
            return None

    monkeypatch.setattr("energy_optimizer.service.HaClient", UnreachableHa)

    first = await service.control_battery(now=now)
    repeated = await service.control_battery(now=now + dt.timedelta(days=1))

    assert first["result"] == "reconnect_restore_unverified"
    assert repeated["result"] == "lockout"
    assert connection_attempts == ["constructed"]
    with store.session() as session:
        state = session.get(ControllerStateRow, "current")
        assert state is not None
        assert state.state == "LOCKOUT"
        assert state.lockout_reason == "path_loss_recovery_attempted_unverified"


async def test_publish_battery_heartbeat_updates_state() -> None:
    settings = _settings()
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    await service.publish_battery_heartbeat()
    with store.session() as session:
        state = session.get(ControllerStateRow, "current")
        assert state is not None
        assert state.last_heartbeat_at is not None
