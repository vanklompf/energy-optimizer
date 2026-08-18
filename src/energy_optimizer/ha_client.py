"""Home Assistant REST client: telemetry plus verified control primitives.

Read paths expose plant telemetry with Sigen sign normalisation. Control primitives are
idempotent entity operations with typed acknowledgement — they do not choose EMS mode
ordering or arm the inverter.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# HA entity ids (read-only telemetry).
ENTITY_SOC = "sensor.sigen_plant_battery_state_of_charge"
ENTITY_BATTERY_POWER = "sensor.sigen_plant_battery_power"
ENTITY_PV_POWER = "sensor.sigen_plant_pv_power"
ENTITY_CONSUMED_POWER = "sensor.sigen_plant_consumed_power"
ENTITY_GRID_IMPORT_POWER = "sensor.sigen_plant_grid_import_power"
ENTITY_GRID_EXPORT_POWER = "sensor.sigen_plant_grid_export_power"
ENTITY_EMS_MODE = "sensor.sigen_plant_ems_work_mode"
ENTITY_RATED_CAPACITY = "sensor.sigen_plant_rated_energy_capacity"
ENTITY_RATED_CHARGE = "sensor.sigen_plant_ess_rated_charging_power"
ENTITY_RATED_DISCHARGE = "sensor.sigen_plant_ess_rated_discharging_power"

# Fast power sensors are stale after 5 min; SoC allows 10 min. Static rated/EMS values are
# not "stale" merely because they did not change.
POWER_STALE_SECONDS = 5 * 60
SOC_STALE_SECONDS = 10 * 60

_UNAVAILABLE = {"unknown", "unavailable", "none", "", None}
_TOKEN_RE = re.compile(r"(Bearer\s+)(\S+)", re.IGNORECASE)


class AckStatus(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    UNACKNOWLEDGED = "UNACKNOWLEDGED"
    MISMATCH = "MISMATCH"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    HA_REJECTED = "HA_REJECTED"
    UNAVAILABLE = "UNAVAILABLE"
    VALUE_COERCED = "VALUE_COERCED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    MANUAL_OVERWRITE = "MANUAL_OVERWRITE"
    IDEMPOTENT_NOOP = "IDEMPOTENT_NOOP"


@dataclass(slots=True)
class HaState:
    entity_id: str
    state: str
    last_updated: dt.datetime | None
    attributes: dict[str, Any]
    last_changed: dt.datetime | None = None

    def as_float(self) -> float | None:
        if self.state in _UNAVAILABLE:
            return None
        try:
            return float(self.state)
        except (TypeError, ValueError):
            return None


@dataclass(slots=True)
class TelemetrySnapshot:
    ts: dt.datetime
    soc_pct: float | None
    batt_charge_kw: float | None
    batt_discharge_kw: float | None
    pv_kw: float | None
    load_kw: float | None
    grid_import_kw: float | None
    grid_export_kw: float | None
    ems_mode: str | None
    stale: bool
    stale_reasons: list[str]


@dataclass(slots=True)
class ControlEntitySpec:
    entity_id: str
    kind: str  # switch | select | number


@dataclass(slots=True)
class ControlSnapshot:
    ts: dt.datetime
    states: dict[str, HaState | None]
    validation_errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AckResult:
    status: AckStatus
    entity_id: str
    requested: str | float | bool | None
    observed: str | float | bool | None
    detail: str = ""
    latency_ms: float = 0.0


class HaError(RuntimeError):
    def __str__(self) -> str:
        return redact_secrets(super().__str__())


class HaClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify_ssl: bool = True,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        max_retries: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._max_retries = max_retries
        self._verify_ssl = verify_ssl
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> HaClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, verify=self._verify_ssl)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    async def get_state(self, entity_id: str) -> HaState | None:
        client = self._require_client()
        url = f"{self._base_url}/api/states/{entity_id}"
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return _parse_state(resp.json())
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "HA get_state(%s) failed (attempt %d/%d): %s",
                    entity_id,
                    attempt,
                    self._max_retries,
                    redact_secrets(str(exc)),
                )
        raise HaError(f"HA get_state({entity_id}) failed") from last_exc

    async def get_states(self, entity_ids: list[str]) -> dict[str, HaState | None]:
        result: dict[str, HaState | None] = {}
        for eid in entity_ids:
            result[eid] = await self.get_state(eid)
        return result

    async def get_history(
        self, entity_id: str, start: dt.datetime, end: dt.datetime | None = None
    ) -> list[HaState]:
        """Fetch recorder history for an entity between start and end (UTC)."""
        client = self._require_client()
        start_iso = _to_iso(start)
        url = f"{self._base_url}/api/history/period/{start_iso}"
        params: dict[str, str] = {"filter_entity_id": entity_id, "minimal_response": "false"}
        if end is not None:
            params["end_time"] = _to_iso(end)
        resp = await client.get(url, headers=self._headers(), params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return []
        return [_parse_state(item) for item in data[0]]

    async def snapshot(self, now: dt.datetime | None = None) -> TelemetrySnapshot:
        """Fetch the live telemetry snapshot with sign normalisation and staleness flags."""
        now = now or dt.datetime.now(tz=dt.UTC)
        states = await self.get_states(
            [
                ENTITY_SOC,
                ENTITY_BATTERY_POWER,
                ENTITY_PV_POWER,
                ENTITY_CONSUMED_POWER,
                ENTITY_GRID_IMPORT_POWER,
                ENTITY_GRID_EXPORT_POWER,
                ENTITY_EMS_MODE,
            ]
        )
        return build_snapshot(states, now)

    async def get_control_snapshot(
        self,
        specs: list[ControlEntitySpec],
        *,
        now: dt.datetime | None = None,
    ) -> ControlSnapshot:
        """Fetch and validate configured control entities in one snapshot."""
        now = now or dt.datetime.now(tz=dt.UTC)
        states = await self.get_states([spec.entity_id for spec in specs])
        errors: list[str] = []
        for spec in specs:
            state = states.get(spec.entity_id)
            errors.extend(validate_control_entity(spec, state))
        return ControlSnapshot(ts=now, states=states, validation_errors=errors)

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """Call a Home Assistant service and raise ``HaError`` on failure."""
        client = self._require_client()
        url = f"{self._base_url}/api/services/{domain}/{service}"
        try:
            response = await client.post(url, headers=self._headers(), json=data)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HaError(
                f"HA service {domain}.{service} rejected: HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise HaError(f"HA service {domain}.{service} failed: {exc}") from exc

    async def set_number(
        self,
        entity_id: str,
        value: float,
        *,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.2,
        abs_tolerance: float = 0.001,
        cancel_event: asyncio.Event | None = None,
    ) -> AckResult:
        """Set a number entity and classify acknowledgement (never trust fallback 0.0 alone)."""
        t0 = time.perf_counter()
        before = await self.get_state(entity_id)
        if before is None or before.state in _UNAVAILABLE:
            return AckResult(
                AckStatus.UNAVAILABLE, entity_id, value, None, "entity unavailable", _ms(t0)
            )
        current = before.as_float()
        if current is not None and abs(current - value) <= abs_tolerance:
            return AckResult(
                AckStatus.IDEMPOTENT_NOOP, entity_id, value, current, "already set", _ms(t0)
            )
        try:
            await self.call_service("number", "set_value", {"entity_id": entity_id, "value": value})
        except HaError as exc:
            status = (
                AckStatus.HA_REJECTED
                if "rejected" in str(exc).lower() or "HTTP" in str(exc)
                else AckStatus.TRANSPORT_FAILURE
            )
            return AckResult(status, entity_id, value, None, str(exc), _ms(t0))

        return await self._poll_number(
            entity_id,
            value,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            abs_tolerance=abs_tolerance,
            cancel_event=cancel_event,
            previous=current,
            previous_updated=before.last_updated,
        )

    async def select_option(
        self,
        entity_id: str,
        option: str,
        *,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.2,
        cancel_event: asyncio.Event | None = None,
    ) -> AckResult:
        t0 = time.perf_counter()
        before = await self.get_state(entity_id)
        if before is None or before.state in _UNAVAILABLE:
            return AckResult(
                AckStatus.UNAVAILABLE, entity_id, option, None, "entity unavailable", _ms(t0)
            )
        options = before.attributes.get("options") or []
        if options and option not in options:
            return AckResult(
                AckStatus.MISMATCH,
                entity_id,
                option,
                before.state,
                f"option not in {options!r}",
                _ms(t0),
            )
        if before.state == option:
            return AckResult(
                AckStatus.IDEMPOTENT_NOOP, entity_id, option, option, "already selected", _ms(t0)
            )
        try:
            await self.call_service(
                "select", "select_option", {"entity_id": entity_id, "option": option}
            )
        except HaError as exc:
            status = AckStatus.HA_REJECTED if "HTTP" in str(exc) else AckStatus.TRANSPORT_FAILURE
            return AckResult(status, entity_id, option, None, str(exc), _ms(t0))
        return await self._poll_state_equals(
            entity_id,
            option,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            cancel_event=cancel_event,
            previous=before.state,
            t0=t0,
        )

    async def turn_switch(
        self,
        entity_id: str,
        on: bool,
        *,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.2,
        cancel_event: asyncio.Event | None = None,
    ) -> AckResult:
        t0 = time.perf_counter()
        desired = "on" if on else "off"
        before = await self.get_state(entity_id)
        if before is None or before.state in _UNAVAILABLE:
            return AckResult(
                AckStatus.UNAVAILABLE, entity_id, desired, None, "entity unavailable", _ms(t0)
            )
        if before.state == desired:
            return AckResult(
                AckStatus.IDEMPOTENT_NOOP, entity_id, desired, desired, "already set", _ms(t0)
            )
        service = "turn_on" if on else "turn_off"
        try:
            await self.call_service("switch", service, {"entity_id": entity_id})
        except HaError as exc:
            status = AckStatus.HA_REJECTED if "HTTP" in str(exc) else AckStatus.TRANSPORT_FAILURE
            return AckResult(status, entity_id, desired, None, str(exc), _ms(t0))
        return await self._poll_state_equals(
            entity_id,
            desired,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            cancel_event=cancel_event,
            previous=before.state,
            t0=t0,
        )

    async def _poll_number(
        self,
        entity_id: str,
        value: float,
        *,
        timeout_s: float,
        poll_interval_s: float,
        abs_tolerance: float,
        cancel_event: asyncio.Event | None,
        previous: float | None,
        previous_updated: dt.datetime | None,
    ) -> AckResult:
        t0 = time.perf_counter()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return AckResult(AckStatus.CANCELLED, entity_id, value, None, "cancelled", _ms(t0))
            state = await self.get_state(entity_id)
            if state is None or state.state in _UNAVAILABLE:
                await asyncio.sleep(poll_interval_s)
                continue
            observed = state.as_float()
            if observed is None:
                await asyncio.sleep(poll_interval_s)
                continue
            # Contract: HTTP 200 + fallback 0.0 is not acknowledgement of a non-zero write.
            if abs(value) > abs_tolerance and abs(observed) <= abs_tolerance:
                return AckResult(
                    AckStatus.UNACKNOWLEDGED,
                    entity_id,
                    value,
                    observed,
                    "fallback_zero_readback",
                    _ms(t0),
                )
            # A matching cached value cannot acknowledge the service just sent.  Require
            # HA to expose a strictly newer state from this polling sequence.
            if (
                previous_updated is None
                or state.last_updated is None
                or state.last_updated <= previous_updated
            ):
                return AckResult(
                    AckStatus.UNACKNOWLEDGED,
                    entity_id,
                    value,
                    observed,
                    "stale_readback",
                    _ms(t0),
                )
            if abs(observed - value) <= abs_tolerance:
                return AckResult(AckStatus.ACKNOWLEDGED, entity_id, value, observed, "", _ms(t0))
            if abs(observed - value) <= max(abs_tolerance * 10, 0.01):
                return AckResult(
                    AckStatus.VALUE_COERCED, entity_id, value, observed, "rounded", _ms(t0)
                )
            if (
                previous is not None
                and abs(observed - previous) > abs_tolerance
                and abs(observed - value) > abs_tolerance
            ):
                return AckResult(
                    AckStatus.MANUAL_OVERWRITE,
                    entity_id,
                    value,
                    observed,
                    "state changed away from request",
                    _ms(t0),
                )
            await asyncio.sleep(poll_interval_s)
        final = await self.get_state(entity_id)
        observed = final.as_float() if final else None
        return AckResult(AckStatus.TIMEOUT, entity_id, value, observed, "poll timeout", _ms(t0))

    async def _poll_state_equals(
        self,
        entity_id: str,
        desired: str,
        *,
        timeout_s: float,
        poll_interval_s: float,
        cancel_event: asyncio.Event | None,
        previous: str | None,
        t0: float,
    ) -> AckResult:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return AckResult(
                    AckStatus.CANCELLED, entity_id, desired, None, "cancelled", _ms(t0)
                )
            state = await self.get_state(entity_id)
            if state is None or state.state in _UNAVAILABLE:
                await asyncio.sleep(poll_interval_s)
                continue
            if state.state == desired:
                return AckResult(
                    AckStatus.ACKNOWLEDGED, entity_id, desired, state.state, "", _ms(t0)
                )
            if previous is not None and state.state != previous and state.state != desired:
                return AckResult(
                    AckStatus.MANUAL_OVERWRITE,
                    entity_id,
                    desired,
                    state.state,
                    "state changed away from request",
                    _ms(t0),
                )
            await asyncio.sleep(poll_interval_s)
        final = await self.get_state(entity_id)
        observed = final.state if final else None
        if observed == desired:
            return AckResult(AckStatus.ACKNOWLEDGED, entity_id, desired, observed, "", _ms(t0))
        return AckResult(AckStatus.TIMEOUT, entity_id, desired, observed, "poll timeout", _ms(t0))

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise HaError("HaClient used outside of an async context manager")
        return self._client


def validate_control_entity(spec: ControlEntitySpec, state: HaState | None) -> list[str]:
    if state is None:
        return [f"{spec.entity_id}: missing"]
    if state.state in _UNAVAILABLE:
        return [f"{spec.entity_id}: unavailable"]
    errors: list[str] = []
    if spec.kind == "select":
        options = state.attributes.get("options")
        if not isinstance(options, list) or not options:
            errors.append(f"{spec.entity_id}: select options missing")
        elif state.state not in options:
            errors.append(f"{spec.entity_id}: state not in options")
    elif spec.kind == "number":
        minimum = state.attributes.get("min")
        maximum = state.attributes.get("max")
        step = state.attributes.get("step")
        value = state.as_float()
        if value is None:
            errors.append(f"{spec.entity_id}: non-numeric state")
        if minimum is None or maximum is None:
            errors.append(f"{spec.entity_id}: number bounds missing")
        if step is None:
            errors.append(f"{spec.entity_id}: number step missing")
    elif spec.kind == "switch":
        if state.state not in {"on", "off"}:
            errors.append(f"{spec.entity_id}: switch state invalid")
    return errors


def redact_secrets(message: str) -> str:
    return _TOKEN_RE.sub(r"\1***", message)


def build_snapshot(states: dict[str, HaState | None], now: dt.datetime) -> TelemetrySnapshot:
    soc = states.get(ENTITY_SOC)
    batt = states.get(ENTITY_BATTERY_POWER)
    pv = states.get(ENTITY_PV_POWER)
    load = states.get(ENTITY_CONSUMED_POWER)
    grid_in = states.get(ENTITY_GRID_IMPORT_POWER)
    grid_out = states.get(ENTITY_GRID_EXPORT_POWER)
    ems = states.get(ENTITY_EMS_MODE)

    batt_kw = batt.as_float() if batt else None
    charge_kw, discharge_kw = _split_battery_power(batt_kw)

    stale_reasons: list[str] = []
    if _is_stale(soc, now, SOC_STALE_SECONDS, peer_fresh=_integration_is_live(states, now)):
        stale_reasons.append("soc telemetry stale (>10min) or missing")
    for name, st in (
        ("battery power", batt),
        ("pv power", pv),
        ("load power", load),
        ("grid import", grid_in),
        ("grid export", grid_out),
    ):
        if _is_stale(st, now, POWER_STALE_SECONDS, zero_is_fresh=True):
            stale_reasons.append(f"{name} telemetry stale (>5min) or missing")

    return TelemetrySnapshot(
        ts=now,
        soc_pct=soc.as_float() if soc else None,
        batt_charge_kw=charge_kw,
        batt_discharge_kw=discharge_kw,
        pv_kw=pv.as_float() if pv else None,
        load_kw=load.as_float() if load else None,
        grid_import_kw=grid_in.as_float() if grid_in else None,
        grid_export_kw=grid_out.as_float() if grid_out else None,
        ems_mode=ems.state if ems and ems.state not in _UNAVAILABLE else None,
        stale=bool(stale_reasons),
        stale_reasons=stale_reasons,
    )


def _split_battery_power(batt_kw: float | None) -> tuple[float | None, float | None]:
    """Sigen convention: >0 charging, <0 discharging. Return (charge_kw, discharge_kw) >= 0."""
    if batt_kw is None:
        return None, None
    if batt_kw >= 0:
        return batt_kw, 0.0
    return 0.0, -batt_kw


def _integration_is_live(states: dict[str, HaState | None], now: dt.datetime) -> bool:
    """True when a non-SoC Sigen entity reported recently.

    A full or idle battery pins SoC, so HA stops emitting updates for it. Age alone is
    then not evidence of a dead feed; another entity from the same integration still
    moving is. Requiring that proof keeps a genuinely dead SoC feed stale.
    """
    for entity_id in (
        ENTITY_BATTERY_POWER,
        ENTITY_PV_POWER,
        ENTITY_CONSUMED_POWER,
        ENTITY_GRID_IMPORT_POWER,
        ENTITY_GRID_EXPORT_POWER,
        ENTITY_EMS_MODE,
    ):
        state = states.get(entity_id)
        if state is None or state.state in _UNAVAILABLE or state.last_updated is None:
            continue
        if (now - state.last_updated).total_seconds() <= POWER_STALE_SECONDS:
            return True
    return False


def _is_stale(
    state: HaState | None,
    now: dt.datetime,
    threshold_s: int,
    *,
    zero_is_fresh: bool = False,
    peer_fresh: bool = False,
) -> bool:
    if state is None or state.state in _UNAVAILABLE:
        return True
    # HA power sensors commonly stop emitting updates when their value is pinned at zero
    # (PV overnight, an idle battery, no grid export). A valid numeric zero is a legitimate
    # steady state, so its age must not mark the whole snapshot stale and block a run.
    if zero_is_fresh:
        value = state.as_float()
        if value is not None and abs(value) < 1e-9:
            return False
    if state.last_updated is None:
        return True
    age = (now - state.last_updated).total_seconds()
    if age <= threshold_s:
        return False
    return not peer_fresh


def _parse_state(payload: dict[str, Any]) -> HaState:
    last_changed = _parse_ha_timestamp(payload.get("last_changed"))
    last_updated = _parse_ha_timestamp(payload.get("last_updated")) or last_changed
    return HaState(
        entity_id=payload.get("entity_id", ""),
        state=payload.get("state", ""),
        last_updated=last_updated,
        attributes=payload.get("attributes", {}) or {},
        last_changed=last_changed,
    )


def _parse_ha_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat()


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0
