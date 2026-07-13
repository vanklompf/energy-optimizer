"""Home Assistant REST client: live states + history, with staleness detection.

Read-only in the MVP. Sign normalisation for the Sigen battery power is explicit and matches
live verification: ``sensor.sigen_plant_battery_power > 0`` while charging, ``< 0`` while
discharging. Internally we expose separate non-negative charge/discharge values.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# HA entity ids (read-only in MVP).
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


@dataclass(slots=True)
class HaState:
    entity_id: str
    state: str
    last_updated: dt.datetime | None
    attributes: dict[str, Any]

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


class HaError(RuntimeError):
    pass


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
                    exc,
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

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise HaError("HaClient used outside of an async context manager")
        return self._client


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
    if _is_stale(soc, now, SOC_STALE_SECONDS):
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


def _is_stale(
    state: HaState | None,
    now: dt.datetime,
    threshold_s: int,
    *,
    zero_is_fresh: bool = False,
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
    return age > threshold_s


def _parse_state(payload: dict[str, Any]) -> HaState:
    last_updated = payload.get("last_updated") or payload.get("last_changed")
    ts: dt.datetime | None = None
    if isinstance(last_updated, str):
        try:
            ts = dt.datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
        except ValueError:
            ts = None
    return HaState(
        entity_id=payload.get("entity_id", ""),
        state=payload.get("state", ""),
        last_updated=ts,
        attributes=payload.get("attributes", {}) or {},
    )


def _to_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat()
