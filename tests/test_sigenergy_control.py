from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pytest

from energy_optimizer.battery_control import BatteryControlIntent, ControlDirection
from energy_optimizer.config import Settings
from energy_optimizer.ha_client import AckResult, AckStatus, HaState
from energy_optimizer.sigenergy_control import PhysicalSnapshot, SigenergyController


@dataclass
class FakeHa:
    states: dict[str, HaState] = field(default_factory=dict)
    number_register_ack_reliable: bool = False
    calls: list[tuple[str, str, dict]] = field(default_factory=list)
    select_ack: AckStatus = AckStatus.ACKNOWLEDGED
    switch_ack: AckStatus = AckStatus.ACKNOWLEDGED
    number_ack: AckStatus = AckStatus.UNACKNOWLEDGED

    async def get_state(self, entity_id: str) -> HaState | None:
        return self.states.get(entity_id)

    async def get_states(self, entity_ids: list[str]) -> dict[str, HaState | None]:
        return {eid: self.states.get(eid) for eid in entity_ids}

    async def call_service(self, domain: str, service: str, data: dict) -> None:
        self.calls.append((domain, service, data))
        entity_id = data.get("entity_id")
        if domain == "switch" and entity_id:
            desired = "on" if service == "turn_on" else "off"
            self.states[entity_id] = _state(entity_id, desired)
        if domain == "select" and entity_id:
            self.states[entity_id] = _state(
                entity_id,
                data["option"],
                attributes=self.states.get(entity_id, _state(entity_id, "")).attributes,
            )
        if domain == "number" and entity_id:
            # Simulate the contract defect: state stays at fallback 0.0 unless reliable.
            shown = str(data["value"]) if self.number_register_ack_reliable else "0.0"
            self.states[entity_id] = _state(entity_id, shown)

    async def select_option(self, entity_id: str, option: str, **kwargs) -> AckResult:
        self.calls.append(("select", "select_option", {"entity_id": entity_id, "option": option}))
        observed = None
        if self.select_ack in {AckStatus.ACKNOWLEDGED, AckStatus.IDEMPOTENT_NOOP}:
            prev = self.states.get(entity_id)
            attrs = prev.attributes if prev else {"options": [option]}
            self.states[entity_id] = _state(entity_id, option, attributes=attrs)
            observed = option
        return AckResult(self.select_ack, entity_id, option, observed)

    async def turn_switch(self, entity_id: str, on: bool, **kwargs) -> AckResult:
        service = "turn_on" if on else "turn_off"
        self.calls.append(("switch", service, {"entity_id": entity_id}))
        desired = "on" if on else "off"
        observed = None
        if self.switch_ack in {AckStatus.ACKNOWLEDGED, AckStatus.IDEMPOTENT_NOOP}:
            self.states[entity_id] = _state(entity_id, desired)
            observed = desired
        return AckResult(self.switch_ack, entity_id, desired, observed)

    async def set_number(self, entity_id: str, value: float, **kwargs) -> AckResult:
        self.calls.append(("number", "set_value", {"entity_id": entity_id, "value": value}))
        if not self.number_register_ack_reliable:
            return AckResult(
                AckStatus.UNACKNOWLEDGED, entity_id, value, 0.0, "UNACKNOWLEDGED_LIMIT"
            )
        self.states[entity_id] = _state(entity_id, str(value))
        return AckResult(self.number_ack, entity_id, value, value)


@dataclass
class FakePhysical:
    sequence: list[PhysicalSnapshot]
    idx: int = 0

    async def read_physical(self) -> PhysicalSnapshot:
        if self.idx < len(self.sequence) - 1:
            snap = self.sequence[self.idx]
            self.idx += 1
            return snap
        return self.sequence[-1]


def _state(entity_id: str, state: str, attributes: dict | None = None) -> HaState:
    return HaState(
        entity_id=entity_id,
        state=state,
        last_updated=dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC),
        attributes=attributes or {},
    )


def _phys(
    battery: float | None,
    *,
    grid_in: float | None = 0.0,
    grid_out: float | None = 0.0,
    soc: float | None = 50.0,
) -> PhysicalSnapshot:
    return PhysicalSnapshot(
        battery_power_kw=battery,
        grid_import_kw=grid_in,
        grid_export_kw=grid_out,
        soc_pct=soc,
    )


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "db": ":memory:",
        "battery_control_grid_charge_enabled": True,
        "battery_control_physical_verify_timeout_seconds": 15.0,
        "battery_control_standby_neutral_band_kw": 0.12,
        "battery_control_command_timeout_seconds": 1.0,
        "battery_control_command_poll_seconds": 0.01,
        "battery_control_retry_limit": 1,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _intent(*, direction=ControlDirection.CHARGE, power=0.5, grid_charge=True, export=False):
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    return BatteryControlIntent(
        source_run_id=1,
        interval_start=now,
        direction=direction,
        requested_power_kw=power,
        cutoff_soc_pct=98.0,
        expiry=now + dt.timedelta(minutes=15),
        grid_charge=grid_charge,
        export=export,
        expected_grid_direction="import" if grid_charge else None,
        expected_grid_kw_min=0.0,
        expected_grid_kw_max=2.0,
        expected_financial_value_pln=0.1,
        reason_codes=("grid_charge_arbitrage",),
    )


def _controller(ha: FakeHa, physical: FakePhysical, settings: Settings | None = None):
    async def _sleep(_s: float) -> None:
        return None

    return SigenergyController(
        ha,  # type: ignore[arg-type]
        settings or _settings(),
        physical=physical,
        sleep=_sleep,
        monotonic=lambda: 0.0,
    )


@pytest.mark.asyncio
async def test_unacknowledged_limit_blocks_before_remote_enable() -> None:
    settings = _settings()
    ha = FakeHa(
        states={
            settings.battery_control_remote_ems_switch_entity: _state(
                settings.battery_control_remote_ems_switch_entity, "off"
            ),
            settings.battery_control_mode_select_entity: _state(
                settings.battery_control_mode_select_entity,
                "Standby",
                attributes={"options": ["Standby", "Command Charging (Grid First)"]},
            ),
        },
        number_register_ack_reliable=False,
    )
    physical = FakePhysical([_phys(-0.05)])
    controller = _controller(ha, physical, settings)
    result = await controller.apply_intent(_intent())
    assert result.control.failure_reason == "UNACKNOWLEDGED_LIMIT"
    assert not any(call[0] == "switch" and call[1] == "turn_on" for call in result.service_calls)


@pytest.mark.asyncio
async def test_discharge_export_blocked() -> None:
    settings = _settings(battery_control_authorize_discharge=False)
    ha = FakeHa(
        states={
            settings.battery_control_remote_ems_switch_entity: _state(
                settings.battery_control_remote_ems_switch_entity, "off"
            )
        }
    )
    physical = FakePhysical([_phys(0.0)])
    controller = _controller(ha, physical, settings)
    result = await controller.apply_intent(
        _intent(direction=ControlDirection.DISCHARGE, grid_charge=False, export=True)
    )
    assert result.control.failure_reason == "discharge_export_not_authorized"
    assert result.service_calls == []


@pytest.mark.asyncio
async def test_characterized_charge_order_when_ack_reliable() -> None:
    settings = _settings()
    ha = FakeHa(
        states={
            settings.battery_control_remote_ems_switch_entity: _state(
                settings.battery_control_remote_ems_switch_entity, "off"
            ),
            settings.battery_control_mode_select_entity: _state(
                settings.battery_control_mode_select_entity,
                "Standby",
                attributes={"options": ["Standby", "Command Charging (Grid First)"]},
            ),
        },
        number_register_ack_reliable=True,
        number_ack=AckStatus.ACKNOWLEDGED,
    )
    physical = FakePhysical(
        [
            _phys(-0.36),
            _phys(0.05),
            _phys(0.5, grid_in=0.9, soc=50.1),
        ]
    )

    # Advance monotonic enough for waits to see successive physical samples once.
    ticks = {"n": 0.0}

    def mono() -> float:
        ticks["n"] += 0.3
        return ticks["n"]

    async def _sleep(_s: float) -> None:
        return None

    controller = SigenergyController(
        ha,  # type: ignore[arg-type]
        settings,
        physical=physical,
        sleep=_sleep,
        monotonic=mono,
    )
    result = await controller.apply_intent(_intent(power=0.5))
    assert result.control.failure_reason is None
    assert result.control.physical_verified is True
    domains_services = [(d, s) for d, s, _ in result.service_calls]
    # Dormant Standby before enable; command mode after.
    assert ("select", "select_option") in domains_services
    assert ("switch", "turn_on") in domains_services
    assert domains_services.index(("select", "select_option")) < domains_services.index(
        ("switch", "turn_on")
    )


@pytest.mark.asyncio
async def test_fallback_turns_remote_ems_off() -> None:
    settings = _settings()
    ha = FakeHa(
        states={
            settings.battery_control_remote_ems_switch_entity: _state(
                settings.battery_control_remote_ems_switch_entity, "on"
            ),
            settings.battery_control_mode_select_entity: _state(
                settings.battery_control_mode_select_entity, "Command Charging (Grid First)"
            ),
        },
        number_register_ack_reliable=False,
    )
    physical = FakePhysical(
        [
            _phys(0.5, grid_in=0.8),
            _phys(0.05),
        ]
    )
    ticks = {"n": 0.0}

    def mono() -> float:
        ticks["n"] += 0.5
        return ticks["n"]

    async def _sleep(_s: float) -> None:
        return None

    controller = SigenergyController(
        ha,  # type: ignore[arg-type]
        settings,
        physical=physical,
        sleep=_sleep,
        monotonic=mono,
    )
    result = await controller.fallback("test")
    assert ("switch", "turn_off") in [(d, s) for d, s, _ in result.service_calls]
    assert ha.states[settings.battery_control_remote_ems_switch_entity].state == "off"


@pytest.mark.asyncio
async def test_unknown_physical_state_does_not_command() -> None:
    settings = _settings()
    ha = FakeHa(
        states={
            settings.battery_control_remote_ems_switch_entity: _state(
                settings.battery_control_remote_ems_switch_entity, "off"
            )
        }
    )
    physical = FakePhysical([_phys(None, grid_in=None, grid_out=None, soc=None)])
    controller = _controller(ha, physical, settings)
    result = await controller.apply_intent(_intent())
    assert result.control.failure_reason == "physical_state_unknown"
    assert result.service_calls == []
