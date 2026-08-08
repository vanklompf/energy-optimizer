"""Reusable Home Assistant emulator for non-actuating battery-control tests."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from energy_optimizer.ha_client import AckResult, AckStatus, HaState
from energy_optimizer.sigenergy_control import PhysicalSnapshot


def ha_state(
    entity_id: str,
    state: str,
    *,
    attributes: dict[str, Any] | None = None,
    ts: dt.datetime | None = None,
) -> HaState:
    now = ts or dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    return HaState(entity_id, state, now, attributes or {}, last_changed=now)


@dataclass
class EmulatedHa:
    """In-memory HA REST stand-in. Never contacts a network."""

    states: dict[str, HaState] = field(default_factory=dict)
    number_register_ack_reliable: bool = False
    calls: list[tuple[str, str, dict]] = field(default_factory=list)
    select_ack: AckStatus = AckStatus.ACKNOWLEDGED
    switch_ack: AckStatus = AckStatus.ACKNOWLEDGED
    number_ack: AckStatus = AckStatus.ACKNOWLEDGED
    fail_service: tuple[str, str] | None = None
    http_error: Exception | None = None
    disconnect_after_calls: int | None = None

    async def get_state(self, entity_id: str) -> HaState | None:
        self._maybe_fail()
        return self.states.get(entity_id)

    async def get_states(self, entity_ids: list[str]) -> dict[str, HaState | None]:
        self._maybe_fail()
        return {eid: self.states.get(eid) for eid in entity_ids}

    async def call_service(self, domain: str, service: str, data: dict) -> None:
        self._maybe_fail(domain, service)
        self.calls.append((domain, service, data))
        entity_id = data.get("entity_id")
        if domain == "switch" and entity_id:
            desired = "on" if service == "turn_on" else "off"
            self.states[entity_id] = ha_state(entity_id, desired)
        if domain == "select" and entity_id:
            prev = self.states.get(entity_id)
            attrs = prev.attributes if prev else {"options": [data["option"]]}
            self.states[entity_id] = ha_state(entity_id, data["option"], attributes=attrs)
        if domain == "number" and entity_id:
            shown = str(data["value"]) if self.number_register_ack_reliable else "0.0"
            self.states[entity_id] = ha_state(entity_id, shown)

    async def select_option(self, entity_id: str, option: str, **kwargs) -> AckResult:
        await self.call_service(
            "select", "select_option", {"entity_id": entity_id, "option": option}
        )
        observed = None
        if self.select_ack in {AckStatus.ACKNOWLEDGED, AckStatus.IDEMPOTENT_NOOP}:
            observed = option
        return AckResult(self.select_ack, entity_id, option, observed)

    async def turn_switch(self, entity_id: str, on: bool, **kwargs) -> AckResult:
        service = "turn_on" if on else "turn_off"
        await self.call_service("switch", service, {"entity_id": entity_id})
        desired = "on" if on else "off"
        observed = desired if self.switch_ack in {
            AckStatus.ACKNOWLEDGED,
            AckStatus.IDEMPOTENT_NOOP,
        } else None
        return AckResult(self.switch_ack, entity_id, desired, observed)

    async def set_number(self, entity_id: str, value: float, **kwargs) -> AckResult:
        await self.call_service(
            "number", "set_value", {"entity_id": entity_id, "value": value}
        )
        if not self.number_register_ack_reliable:
            return AckResult(
                AckStatus.UNACKNOWLEDGED, entity_id, value, 0.0, "UNACKNOWLEDGED_LIMIT"
            )
        return AckResult(self.number_ack, entity_id, value, value)

    def _maybe_fail(self, domain: str | None = None, service: str | None = None) -> None:
        if self.http_error is not None:
            raise self.http_error
        if self.disconnect_after_calls is not None and len(self.calls) >= self.disconnect_after_calls:
            raise ConnectionError("emulated HA disconnect")
        if self.fail_service and domain and service and (domain, service) == self.fail_service:
            raise RuntimeError(f"emulated HA {domain}.{service} failure")


@dataclass
class EmulatedPhysical:
    sequence: list[PhysicalSnapshot]
    idx: int = 0

    async def read_physical(self) -> PhysicalSnapshot:
        if self.idx >= len(self.sequence):
            return self.sequence[-1]
        snap = self.sequence[self.idx]
        self.idx += 1
        return snap


def phys(
    battery_power_kw: float | None,
    *,
    grid_in: float | None = 0.0,
    grid_out: float | None = 0.0,
    soc: float | None = 50.0,
    ems_mode: str | None = None,
) -> PhysicalSnapshot:
    return PhysicalSnapshot(
        battery_power_kw=battery_power_kw,
        grid_import_kw=grid_in,
        grid_export_kw=grid_out,
        soc_pct=soc,
        ems_mode=ems_mode,
    )
