from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from energy_optimizer.battery_control import ControlDirection
from energy_optimizer.config import Settings
from energy_optimizer.control_store import try_acquire_lease
from energy_optimizer.ha_client import HaError, HaState
from energy_optimizer.safety import SafetyReport, Status
from energy_optimizer.service import Service, watchdog_health_from_ha
from energy_optimizer.store import (
    ControlAction,
    ControllerStateRow,
    PlanStep,
    Run,
    Store,
    Telemetry,
    utcnow,
)

_WATCHDOG_READY = "binary_sensor.pvopti_battery_control_watchdog_ready"
_WATCHDOG_ACK = "input_datetime.pvopti_battery_control_last_heartbeat"


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


def test_watchdog_health_requires_ready_signal_and_fresh_ha_ack() -> None:
    now = dt.datetime(2026, 8, 16, 17, 0, tzinfo=dt.UTC)
    health = HaState(_WATCHDOG_READY, "on", now, {})
    # HA input_datetime stores local Europe/Warsaw wall-clock values.
    ack = HaState(_WATCHDOG_ACK, "2026-08-16 19:00:00", now, {})

    healthy, reason = watchdog_health_from_ha(
        health,
        ack,
        now=now,
        timezone="Europe/Warsaw",
        expiry_seconds=60,
    )

    assert healthy is True
    assert reason == "ok"


def test_watchdog_health_fails_closed_for_stale_or_unready_ha_ack() -> None:
    now = dt.datetime(2026, 8, 16, 17, 2, tzinfo=dt.UTC)
    ready = HaState(_WATCHDOG_READY, "on", now, {})
    stale_ack = HaState(_WATCHDOG_ACK, "2026-08-16 19:00:00", now, {})
    not_ready = HaState(_WATCHDOG_READY, "off", now, {})
    fresh_ack = HaState(_WATCHDOG_ACK, "2026-08-16 19:02:00", now, {})

    assert watchdog_health_from_ha(
        ready, stale_ack, now=now, timezone="Europe/Warsaw", expiry_seconds=60
    ) == (False, "watchdog_ack_stale")
    assert watchdog_health_from_ha(
        not_ready, fresh_ack, now=now, timezone="Europe/Warsaw", expiry_seconds=60
    ) == (False, "watchdog_not_ready")


async def test_service_watchdog_health_requires_independent_ha_ready_and_ack(monkeypatch) -> None:
    settings = _settings().model_copy(
        update={
            "ha_token": "token",
            "battery_control_watchdog_health_entity": _WATCHDOG_READY,
            "battery_control_watchdog_ack_entity": _WATCHDOG_ACK,
        }
    )
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    now = dt.datetime(2026, 8, 16, 17, 0, tzinfo=dt.UTC)

    class FakeHa:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get_states(self, entity_ids):
            assert entity_ids == [
                settings.battery_control_watchdog_health_entity,
                settings.battery_control_watchdog_ack_entity,
            ]
            return {
                settings.battery_control_watchdog_health_entity: HaState(
                    entity_ids[0], "on", now, {}
                ),
                settings.battery_control_watchdog_ack_entity: HaState(
                    entity_ids[1], "2026-08-16 19:00:00", now, {}
                ),
            }

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", FakeHa)

    assert await service._watchdog_health(now) == (True, "ok")


async def test_service_watchdog_health_fails_closed_when_ha_read_is_unavailable(
    monkeypatch,
) -> None:
    settings = _settings().model_copy(
        update={
            "ha_token": "token",
            "battery_control_watchdog_health_entity": _WATCHDOG_READY,
            "battery_control_watchdog_ack_entity": _WATCHDOG_ACK,
        }
    )
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

        async def get_states(self, entity_ids):
            raise HaError("HA unavailable")

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", FailingHa)

    assert await service._watchdog_health(dt.datetime(2026, 8, 16, 17, 0, tzinfo=dt.UTC)) == (
        False,
        "watchdog_health_unavailable",
    )


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

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", BoomHa)

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

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", BoomHa)

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

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", FakeHa)
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


def _export_plan_service(settings: Settings, now: dt.datetime, interval_start: dt.datetime):
    """Service whose only plan step wants export, which the manual request must override."""
    store = Store(":memory:")
    store.create_all()
    with store.session() as session:
        session.add(
            Run(
                run_id="run-export",
                ts=now,
                mode="dry_run",
                horizon_hours=27,
                known_price_hours=27,
                status="ok",
            )
        )
        session.add(
            PlanStep(
                run_id="run-export",
                interval_start=interval_start,
                dt_hours=0.25,
                battery_to_grid_kwh=1.5,
                battery_to_load_kwh=0.04,
            )
        )
    return Service(settings, store)


async def test_manual_charge_request_overrides_plan_export_interval() -> None:
    settings = _settings(step_minutes=15, battery_control_grid_charge_enabled=True)
    now = dt.datetime(2026, 8, 17, 18, 50, tzinfo=dt.UTC)
    interval_start = dt.datetime(2026, 8, 17, 18, 45, tzinfo=dt.UTC)
    service = _export_plan_service(settings, now, interval_start)

    service.request_manual_charge(target_kw=2.0, duration_seconds=600, now=now)
    intent, run_id, selected_start, _ = service.build_battery_control_intent(now)

    assert intent.direction is ControlDirection.CHARGE
    assert intent.grid_charge is True
    assert intent.export is False
    assert intent.requested_power_kw == 2.0
    assert intent.reason_codes == ("manual_grid_charge_test",)
    # Verification depends on these bounds, so the manual intent must carry them.
    assert intent.expected_grid_direction == "import"
    assert intent.expected_grid_kw_max == settings.battery_control_max_grid_import_kw
    assert intent.cutoff_soc_pct == settings.battery_control_max_soc_pct
    assert run_id == "run-export"
    assert selected_start == interval_start


async def test_manual_charge_runs_when_plan_has_no_current_interval() -> None:
    settings = _settings(step_minutes=15, battery_control_grid_charge_enabled=True)
    now = dt.datetime(2026, 8, 17, 19, 16, tzinfo=dt.UTC)
    future = dt.datetime(2026, 8, 17, 19, 30, tzinfo=dt.UTC)
    service = _export_plan_service(settings, now, future)

    service.request_manual_charge(target_kw=2.0, duration_seconds=600, now=now)
    intent, _run_id, interval_start, _plan_age = service.build_battery_control_intent(now)

    assert intent.direction is ControlDirection.CHARGE
    assert intent.grid_charge is True
    assert intent.requested_power_kw == 2.0
    assert interval_start == dt.datetime(2026, 8, 17, 19, 15, tzinfo=dt.UTC)


async def test_manual_charge_request_expires_back_to_plan_intent() -> None:
    settings = _settings(step_minutes=15, battery_control_grid_charge_enabled=True)
    now = dt.datetime(2026, 8, 17, 18, 46, tzinfo=dt.UTC)
    interval_start = dt.datetime(2026, 8, 17, 18, 45, tzinfo=dt.UTC)
    service = _export_plan_service(settings, now, interval_start)

    service.request_manual_charge(target_kw=4.0, duration_seconds=60, now=now)
    assert service.manual_charge_status(now)["target_kw"] == 4.0

    later = now + dt.timedelta(seconds=120)
    assert service.active_manual_charge(later) is None
    assert service.manual_charge_status(later) is None
    # The plan still wants export, which stays refused while the export gate is off.
    try:
        service.build_battery_control_intent(later)
    except ValueError as exc:
        assert "battery_export_enabled is false" in str(exc)
    else:  # pragma: no cover - regression guard
        raise AssertionError("expected the plan export flow to be refused")


async def test_manual_charge_request_rejects_unsafe_arguments() -> None:
    gated = _settings(battery_control_grid_charge_enabled=False)
    service = Service(gated, Store(":memory:"))
    now = utcnow()

    # A closed gate would make every cycle fail the intent and fall back.
    with pytest.raises(ValueError, match="grid_charge_enabled"):
        service.request_manual_charge(target_kw=2.0, now=now)

    settings = _settings(battery_control_grid_charge_enabled=True)
    service = Service(settings, Store(":memory:"))
    with pytest.raises(ValueError, match="deadband"):
        service.request_manual_charge(target_kw=0.05, now=now)
    with pytest.raises(ValueError, match="max_charge_kw"):
        service.request_manual_charge(target_kw=12.0, now=now)
    with pytest.raises(ValueError, match="duration_seconds"):
        service.request_manual_charge(target_kw=2.0, duration_seconds=7200, now=now)
    assert service.manual_charge_status(now) is None


async def test_manual_charge_ramp_estimate_matches_step_and_cadence() -> None:
    settings = _settings(
        battery_control_grid_charge_enabled=True,
        battery_control_max_power_step_kw=0.5,
        battery_control_cadence_seconds=30.0,
    )
    service = Service(settings, Store(":memory:"))

    # 8.8 kW needs 18 half-kW steps, so 17 cycles after the first command.
    assert service.manual_charge_ramp_seconds(8.8) == 510.0
    assert service.manual_charge_ramp_seconds(0.5) == 0.0


def _discharge_settings(**overrides) -> Settings:
    """Settings that authorize discharge so manual discharge/export can be armed."""
    base = dict(
        step_minutes=15,
        battery_control_authorize_discharge=True,
        battery_control_supported_directions=["FALLBACK", "IDLE", "CHARGE", "DISCHARGE"],
    )
    base.update(overrides)
    return _settings(**base)


async def test_manual_discharge_command_overrides_plan_to_house_load() -> None:
    settings = _discharge_settings()
    now = dt.datetime(2026, 8, 17, 18, 50, tzinfo=dt.UTC)
    interval_start = dt.datetime(2026, 8, 17, 18, 45, tzinfo=dt.UTC)
    service = _export_plan_service(settings, now, interval_start)

    service.request_manual_command(
        direction="DISCHARGE", target_kw=1.5, duration_seconds=600, now=now
    )
    intent, _run_id, selected_start, _ = service.build_battery_control_intent(now)

    assert intent.direction is ControlDirection.DISCHARGE
    assert intent.export is False
    assert intent.requested_power_kw == 1.5
    assert intent.reason_codes == ("manual_discharge_test",)
    # Discharge to load declares no grid flow, so any measured export trips
    # unplanned_export — the safety net checkpoint 2 relies on.
    assert intent.expected_grid_direction is None
    # The operating reserve, not the charge ceiling, guards the discharge floor.
    assert intent.cutoff_soc_pct == settings.battery_control_min_soc_pct
    assert selected_start == interval_start


async def test_manual_export_command_declares_export_bounds() -> None:
    settings = _discharge_settings(battery_export_enabled=True)
    now = dt.datetime(2026, 8, 17, 18, 50, tzinfo=dt.UTC)
    interval_start = dt.datetime(2026, 8, 17, 18, 45, tzinfo=dt.UTC)
    service = _export_plan_service(settings, now, interval_start)

    service.request_manual_command(
        direction="EXPORT", target_kw=0.5, duration_seconds=600, now=now
    )
    intent, _run_id, _start, _ = service.build_battery_control_intent(now)

    assert intent.direction is ControlDirection.DISCHARGE
    assert intent.export is True
    assert intent.requested_power_kw == 0.5
    assert intent.reason_codes == ("manual_export_test",)
    assert intent.expected_grid_direction == "export"
    assert intent.expected_grid_kw_max == settings.battery_control_max_grid_export_kw


async def test_manual_command_rejects_closed_direction_gates() -> None:
    now = utcnow()

    # Discharge without authorization is refused before it can fail every cycle.
    service = Service(_settings(), Store(":memory:"))
    with pytest.raises(ValueError, match="authorize_discharge"):
        service.request_manual_command(direction="DISCHARGE", target_kw=1.0, now=now)

    # Export needs the export gate on top of discharge authorization.
    service = Service(_discharge_settings(), Store(":memory:"))
    with pytest.raises(ValueError, match="battery_export_enabled"):
        service.request_manual_command(direction="EXPORT", target_kw=0.5, now=now)

    # An unknown direction is refused outright.
    service = Service(_discharge_settings(battery_export_enabled=True), Store(":memory:"))
    with pytest.raises(ValueError, match="direction must be one of"):
        service.request_manual_command(direction="SIDEWAYS", target_kw=1.0, now=now)


async def test_manual_command_clamps_discharge_target_to_configured_cap() -> None:
    service = Service(_discharge_settings(), Store(":memory:"))
    now = utcnow()

    with pytest.raises(ValueError, match="deadband"):
        service.request_manual_command(direction="DISCHARGE", target_kw=0.05, now=now)
    with pytest.raises(ValueError, match="max_discharge_kw"):
        service.request_manual_command(direction="DISCHARGE", target_kw=12.0, now=now)
    assert service.manual_command_status(now) is None


async def test_manual_command_status_reports_direction_and_expiry() -> None:
    service = Service(_discharge_settings(battery_export_enabled=True), Store(":memory:"))
    now = dt.datetime(2026, 8, 17, 18, 46, tzinfo=dt.UTC)

    service.request_manual_command(
        direction="EXPORT", target_kw=0.5, duration_seconds=60, now=now
    )
    status = service.manual_command_status(now)
    assert status["direction"] == "EXPORT"
    assert status["target_kw"] == 0.5

    later = now + dt.timedelta(seconds=120)
    assert service.active_manual_command(later) is None
    assert service.manual_command_status(later) is None


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

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", FakeHa)

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

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", FailingHa)

    async def fallback(reason: str, command_id=None):
        return {"result": "fallback_recorded", "reason": reason, "command_id": command_id}

    monkeypatch.setattr(service, "fallback_battery", fallback)

    result = await service.reconcile_battery_on_startup()

    assert result["result"] == "fallback_recorded"
    assert result["reason"] == "startup_remote_ems_unknown"




async def test_fallback_battery_locks_out_when_ha_is_reachable_but_restore_is_unverified(
    monkeypatch,
) -> None:
    """A reachable HA does not make an unverified local restore safe to resume from."""
    data = _settings().model_dump()
    data.update(mode="control", battery_control_enabled=True, ha_token="token")
    settings = Settings.model_construct(**data)
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)

    class ReachableHa:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

    class UnverifiedController:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def fallback(self, *args, **kwargs):
            return SimpleNamespace(
                control=SimpleNamespace(
                    observed_state=SimpleNamespace(value="FALLBACK"),
                    physical_verified=False,
                    latency_ms=1.0,
                )
            )

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", ReachableHa)
    monkeypatch.setattr(
        "energy_optimizer.sigenergy_control.SigenergyController", UnverifiedController
    )

    result = await service.fallback_battery("unit_test_unverified")

    assert result["result"] == "fallback"
    assert result["verified"] is False
    with store.session() as session:
        state = session.get(ControllerStateRow, "current")
        assert state is not None
        assert state.state == "LOCKOUT"
        assert state.lockout_reason == "fallback_unverified"


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

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", FailingHa)

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
    """Reconnect recovery must not resume the current plan during the backoff."""
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    data = _settings().model_dump()
    data.update(
        mode="control",
        battery_control_enabled=True,
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
                # Active backoff: OFF-only restore, no economic actuation.
                lockout_until=now + dt.timedelta(hours=1),
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

        async def apply_intent(self, intent, **kwargs):
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

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", FakeHa)
    monkeypatch.setattr(
        "energy_optimizer.sigenergy_control.SigenergyController", FallbackOnlyController
    )

    recovered = await service.control_battery(now=now)
    later = await service.control_battery(now=now + dt.timedelta(minutes=1))

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
    """An unverified reconnect fallback does not retry during the remaining backoff."""
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    data = _settings().model_dump()
    data.update(
        mode="control",
        battery_control_enabled=True,
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
                lockout_until=now + dt.timedelta(hours=1),
            )
        )

    fallback_attempts: list[str] = []

    async def unverified_fallback(reason: str, *, command_id=None):
        fallback_attempts.append(reason)
        return {"result": "fallback", "reason": reason, "verified": False}

    monkeypatch.setattr(service, "fallback_battery", unverified_fallback)

    first = await service.control_battery(now=now)
    repeated = await service.control_battery(now=now + dt.timedelta(minutes=1))

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
    """A failed reconnect cannot retry indefinitely during the backoff."""
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    data = _settings().model_dump()
    data.update(
        mode="control",
        battery_control_enabled=True,
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
                lockout_until=now + dt.timedelta(hours=1),
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

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", UnreachableHa)

    first = await service.control_battery(now=now)
    repeated = await service.control_battery(now=now + dt.timedelta(minutes=1))

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


def _ramp_service(store: Store, now: dt.datetime, interval: dt.datetime) -> Service:
    """Seed a plan asking for 4 kW of charge across a 15-minute interval."""
    service = Service(_settings(), store)
    with store.session() as session:
        session.add(
            Run(
                run_id="ramp-run",
                ts=now,
                mode="dry_run",
                horizon_hours=48,
                known_price_hours=24,
                status="ok",
            )
        )
        session.add(
            PlanStep(
                run_id="ramp-run",
                interval_start=interval,
                dt_hours=0.25,
                pv_to_battery_kwh=1.0,
                grid_to_load_kwh=0.5,
            )
        )
    return service


async def test_control_battery_ramp_limits_plan_power_to_one_step(monkeypatch) -> None:
    """The loop must apply the ramp limit, not command the plan's full power outright."""
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    store = Store(":memory:")
    store.create_all()
    service = _ramp_service(store, now, dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC))

    class BoomHa:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("shadow mode must not touch Home Assistant")

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", BoomHa)

    result = await service.control_battery(now=now)

    step = service.settings.battery_control_max_power_step_kw
    assert result["result"] == "shadow"
    assert result["direction"] == "CHARGE"
    # No measured battery power, so the ramp baseline is 0 and one step is the whole budget.
    assert result["requested_power_kw"] == step
    with store.session() as session:
        action = session.get(ControlAction, result["command_id"])
        assert action is not None
        assert json.loads(action.intent_json or "{}")["requested_power_kw"] == step


async def test_control_battery_ramp_baseline_follows_measured_battery_power(
    monkeypatch,
) -> None:
    """Ramp headroom grows from what the battery is physically doing."""
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    store = Store(":memory:")
    store.create_all()
    service = _ramp_service(store, now, dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC))
    with store.session() as session:
        session.add(Telemetry(ts=now, soc_pct=50.0, batt_charge_kw=2.0, batt_discharge_kw=0.0))

    class BoomHa:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("shadow mode must not touch Home Assistant")

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", BoomHa)

    result = await service.control_battery(now=now)

    assert result["requested_power_kw"] == 2.0 + service.settings.battery_control_max_power_step_kw


async def test_control_battery_ramp_baseline_ignores_stale_telemetry(monkeypatch) -> None:
    """Stale telemetry is not evidence of current power, so the ramp restarts from zero."""
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    store = Store(":memory:")
    store.create_all()
    service = _ramp_service(store, now, dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC))
    stale_age = service.settings.battery_control_max_telemetry_age_seconds + 60
    with store.session() as session:
        session.add(
            Telemetry(
                ts=now - dt.timedelta(seconds=stale_age),
                soc_pct=50.0,
                batt_charge_kw=4.0,
                batt_discharge_kw=0.0,
            )
        )

    class BoomHa:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("shadow mode must not touch Home Assistant")

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", BoomHa)

    result = await service.control_battery(now=now)

    assert result["requested_power_kw"] == service.settings.battery_control_max_power_step_kw


async def test_control_battery_forwards_persisted_direction_across_cycles(
    monkeypatch,
) -> None:
    """A controller built fresh each cycle must still see the previous direction.

    Without this, charge -> discharge reversal never triggers the neutral dwell in
    production even though the reversal logic itself is correct.
    """
    now = dt.datetime(2026, 8, 8, 12, 7, tzinfo=dt.UTC)
    interval = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    data = _settings().model_dump()
    data.update(
        mode="control",
        battery_control_enabled=True,
        battery_control_authorize_discharge=True,
        battery_control_supported_directions=["FALLBACK", "IDLE", "CHARGE", "DISCHARGE"],
        ha_token="token",
    )
    settings = Settings.model_construct(**data)
    store = Store(":memory:")
    store.create_all()
    service = Service(settings, store)
    with store.session() as session:
        session.add(
            Run(
                run_id="reversal-run",
                ts=now,
                mode="control",
                horizon_hours=48,
                known_price_hours=24,
                status="ok",
            )
        )
        session.add(
            PlanStep(
                run_id="reversal-run",
                interval_start=interval,
                dt_hours=0.25,
                battery_to_load_kwh=1.0,
            )
        )
        session.add(ControllerStateRow(key="current", state="ACTIVE_CHARGE"))

    class FakeHa:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

    seen: dict[str, object] = {}

    class RecordingController:
        def __init__(self, ha, settings) -> None:
            pass

        async def apply_intent(self, intent, *, previous_direction=None, cancel_event=None):
            seen["previous_direction"] = previous_direction
            return SimpleNamespace(
                lockout=False,
                control=SimpleNamespace(
                    observed_state=SimpleNamespace(value="ACTIVE_DISCHARGE"),
                    physical_verified=True,
                    failure_reason=None,
                    latency_ms=1.0,
                ),
            )

    async def _authorized(intent, **kwargs):
        return (
            SafetyReport(status=Status.OK, control_enabled=True, control_authorized=True),
            {"would_authorize": True},
        )

    monkeypatch.setattr("energy_optimizer.battery_loop.HaClient", FakeHa)
    monkeypatch.setattr(
        "energy_optimizer.sigenergy_control.SigenergyController", RecordingController
    )
    monkeypatch.setattr(service, "_evaluate_battery_authorization", _authorized)

    result = await service.control_battery(now=now)

    assert result["result"] == "ok"
    assert seen["previous_direction"] == ControlDirection.CHARGE
