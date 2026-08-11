from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx

from energy_optimizer.ha_client import (
    ENTITY_BATTERY_POWER,
    ENTITY_CONSUMED_POWER,
    ENTITY_EMS_MODE,
    ENTITY_GRID_EXPORT_POWER,
    ENTITY_GRID_IMPORT_POWER,
    ENTITY_PV_POWER,
    ENTITY_SOC,
    AckStatus,
    ControlEntitySpec,
    HaClient,
    HaError,
    HaState,
    _parse_state,
    _split_battery_power,
    build_snapshot,
    redact_secrets,
)


def _state(entity: str, state: str, age_s: int, now: dt.datetime) -> HaState:
    return HaState(
        entity_id=entity,
        state=state,
        last_updated=now - dt.timedelta(seconds=age_s),
        attributes={},
    )


def test_split_battery_power_sign_convention() -> None:
    assert _split_battery_power(3.0) == (3.0, 0.0)  # >0 charging
    assert _split_battery_power(-2.5) == (0.0, 2.5)  # <0 discharging
    assert _split_battery_power(None) == (None, None)


def test_parse_state_preserves_last_changed_separately_from_last_updated() -> None:
    state = _parse_state(
        {
            "entity_id": "switch.garage",
            "state": "on",
            "last_changed": "2026-08-02T10:00:00Z",
            "last_updated": "2026-08-02T10:05:00Z",
            "attributes": {"power": 1800},
        }
    )

    assert state.last_changed == dt.datetime(2026, 8, 2, 10, 0, tzinfo=dt.UTC)
    assert state.last_updated == dt.datetime(2026, 8, 2, 10, 5, tzinfo=dt.UTC)


def test_snapshot_fresh_is_not_stale() -> None:
    now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC)
    states = {
        ENTITY_SOC: _state(ENTITY_SOC, "55", 30, now),
        ENTITY_BATTERY_POWER: _state(ENTITY_BATTERY_POWER, "-1.2", 30, now),
        ENTITY_PV_POWER: _state(ENTITY_PV_POWER, "4.0", 30, now),
        ENTITY_CONSUMED_POWER: _state(ENTITY_CONSUMED_POWER, "1.5", 30, now),
        ENTITY_GRID_IMPORT_POWER: _state(ENTITY_GRID_IMPORT_POWER, "0.0", 30, now),
        ENTITY_GRID_EXPORT_POWER: _state(ENTITY_GRID_EXPORT_POWER, "1.3", 30, now),
        ENTITY_EMS_MODE: _state(ENTITY_EMS_MODE, "Self Consumption", 3600, now),
    }
    snap = build_snapshot(states, now)
    assert snap.stale is False
    assert snap.soc_pct == 55
    assert snap.batt_discharge_kw == 1.2
    assert snap.batt_charge_kw == 0.0
    assert snap.ems_mode == "Self Consumption"


def test_snapshot_stale_power_flags() -> None:
    now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC)
    states = {
        ENTITY_SOC: _state(ENTITY_SOC, "55", 30, now),
        ENTITY_BATTERY_POWER: _state(ENTITY_BATTERY_POWER, "1.0", 400, now),  # >5min
        ENTITY_PV_POWER: _state(ENTITY_PV_POWER, "4.0", 30, now),
        ENTITY_CONSUMED_POWER: _state(ENTITY_CONSUMED_POWER, "1.5", 30, now),
        ENTITY_GRID_IMPORT_POWER: _state(ENTITY_GRID_IMPORT_POWER, "0.0", 30, now),
        ENTITY_GRID_EXPORT_POWER: _state(ENTITY_GRID_EXPORT_POWER, "1.3", 30, now),
        ENTITY_EMS_MODE: _state(ENTITY_EMS_MODE, "Self Consumption", 3600, now),
    }
    snap = build_snapshot(states, now)
    assert snap.stale is True
    assert any("battery power" in r for r in snap.stale_reasons)


def test_snapshot_zero_power_sensor_is_not_stale() -> None:
    # PV pinned at 0 overnight (and an idle battery) stop emitting HA updates; a valid
    # numeric zero must not mark the snapshot stale and block the optimiser.
    now = dt.datetime(2026, 7, 12, 23, 0, tzinfo=dt.UTC)
    states = {
        ENTITY_SOC: _state(ENTITY_SOC, "44", 60, now),
        ENTITY_BATTERY_POWER: _state(ENTITY_BATTERY_POWER, "0.0", 3600, now),
        ENTITY_PV_POWER: _state(ENTITY_PV_POWER, "0.0", 14400, now),
        ENTITY_CONSUMED_POWER: _state(ENTITY_CONSUMED_POWER, "0.2", 30, now),
        ENTITY_GRID_IMPORT_POWER: _state(ENTITY_GRID_IMPORT_POWER, "0.0", 3600, now),
        ENTITY_GRID_EXPORT_POWER: _state(ENTITY_GRID_EXPORT_POWER, "0.0", 3600, now),
        ENTITY_EMS_MODE: _state(ENTITY_EMS_MODE, "Custom", 3600, now),
    }
    snap = build_snapshot(states, now)
    assert snap.stale is False
    assert snap.pv_kw == 0.0


def test_snapshot_missing_soc_is_stale() -> None:
    now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC)
    states = {
        ENTITY_SOC: _state(ENTITY_SOC, "unavailable", 30, now),
        ENTITY_BATTERY_POWER: _state(ENTITY_BATTERY_POWER, "1.0", 30, now),
        ENTITY_PV_POWER: _state(ENTITY_PV_POWER, "4.0", 30, now),
        ENTITY_CONSUMED_POWER: _state(ENTITY_CONSUMED_POWER, "1.5", 30, now),
        ENTITY_GRID_IMPORT_POWER: _state(ENTITY_GRID_IMPORT_POWER, "0.0", 30, now),
        ENTITY_GRID_EXPORT_POWER: _state(ENTITY_GRID_EXPORT_POWER, "1.3", 30, now),
        ENTITY_EMS_MODE: _state(ENTITY_EMS_MODE, "Self Consumption", 3600, now),
    }
    snap = build_snapshot(states, now)
    assert snap.stale is True
    assert snap.soc_pct is None


@respx.mock
async def test_call_service_posts_to_home_assistant() -> None:
    route = respx.post("http://ha.local:8123/api/services/switch/turn_on").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with HaClient("http://ha.local:8123", "secret") as client:
        await client.call_service("switch", "turn_on", {"entity_id": "switch.garage"})

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer secret"
    assert request.content == b'{"entity_id":"switch.garage"}'


@respx.mock
async def test_set_number_unacknowledged_when_capability_disabled() -> None:
    async with HaClient(
        "http://ha.local:8123", "secret", number_register_ack_reliable=False
    ) as client:
        result = await client.set_number("number.sigen_plant_ess_max_charging_limit", 0.5)
    assert result.status == AckStatus.UNACKNOWLEDGED
    assert result.detail == "number_register_ack_unreliable"


@respx.mock
async def test_set_number_fallback_zero_after_http_200_is_unacknowledged() -> None:
    entity = "number.sigen_plant_ess_max_charging_limit"
    respx.get(f"http://ha.local:8123/api/states/{entity}").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "entity_id": entity,
                    "state": "8.8",
                    "attributes": {"min": 0, "max": 100, "step": 0.001},
                    "last_updated": "2026-08-08T10:00:00Z",
                },
            ),
            httpx.Response(
                200,
                json={
                    "entity_id": entity,
                    "state": "0.0",
                    "attributes": {"min": 0, "max": 100, "step": 0.001},
                    "last_updated": "2026-08-08T10:00:01Z",
                },
            ),
        ]
    )
    respx.post("http://ha.local:8123/api/services/number/set_value").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with HaClient(
        "http://ha.local:8123", "secret", number_register_ack_reliable=True
    ) as client:
        result = await client.set_number(entity, 0.5, timeout_s=0.5, poll_interval_s=0.01)
    assert result.status == AckStatus.UNACKNOWLEDGED
    assert result.detail == "fallback_zero_readback"


@respx.mock
async def test_set_number_matching_readback_with_unchanged_timestamp_is_unacknowledged() -> None:
    """A matching cached value is not evidence that the just-issued write reached Sigen."""
    entity = "number.sigen_plant_ess_max_charging_limit"
    stale = "2026-08-08T10:00:00Z"
    respx.get(f"http://ha.local:8123/api/states/{entity}").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "entity_id": entity,
                    "state": "8.8",
                    "attributes": {"min": 0, "max": 100, "step": 0.001},
                    "last_updated": stale,
                },
            ),
            httpx.Response(
                200,
                json={
                    "entity_id": entity,
                    "state": "0.5",
                    "attributes": {"min": 0, "max": 100, "step": 0.001},
                    "last_updated": stale,
                },
            ),
        ]
    )
    respx.post("http://ha.local:8123/api/services/number/set_value").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with HaClient(
        "http://ha.local:8123", "secret", number_register_ack_reliable=True
    ) as client:
        result = await client.set_number(entity, 0.5, timeout_s=0.5, poll_interval_s=0.01)

    assert result.status == AckStatus.UNACKNOWLEDGED
    assert result.detail == "stale_readback"


@respx.mock
async def test_set_number_unavailable_post_write_readback_times_out_unacknowledged() -> None:
    """An unavailable HA number after a successful POST cannot acknowledge a register write."""
    entity = "number.sigen_plant_ess_max_charging_limit"
    respx.get(f"http://ha.local:8123/api/states/{entity}").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "entity_id": entity,
                    "state": "8.8",
                    "attributes": {"min": 0, "max": 100, "step": 0.001},
                    "last_updated": "2026-08-08T10:00:00Z",
                },
            ),
            httpx.Response(
                200,
                json={
                    "entity_id": entity,
                    "state": None,
                    "attributes": {"min": 0, "max": 100, "step": 0.001},
                    "last_updated": "2026-08-08T10:00:01Z",
                },
            ),
        ]
    )
    respx.post("http://ha.local:8123/api/services/number/set_value").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with HaClient(
        "http://ha.local:8123", "secret", number_register_ack_reliable=True
    ) as client:
        result = await client.set_number(entity, 0.5, timeout_s=0.0, poll_interval_s=0.01)

    assert result.status == AckStatus.TIMEOUT
    assert result.observed is None
    assert result.detail == "poll timeout"


@respx.mock
async def test_select_option_and_switch_ack_matrix() -> None:
    select_id = "select.sigen_plant_remote_ems_control_mode"
    switch_id = "switch.sigen_plant_remote_ems_controlled_by_home_assistant"
    options = ["Standby", "Command Charging (Grid First)"]

    def _select_payload(state: str) -> dict:
        return {
            "entity_id": select_id,
            "state": state,
            "attributes": {"options": options},
            "last_updated": "2026-08-08T10:00:00Z",
        }

    respx.get(f"http://ha.local:8123/api/states/{select_id}").mock(
        side_effect=[
            httpx.Response(200, json=_select_payload("Standby")),  # mismatch check
            httpx.Response(200, json=_select_payload("Standby")),  # before select
            httpx.Response(200, json=_select_payload("Command Charging (Grid First)")),
        ]
    )
    respx.post("http://ha.local:8123/api/services/select/select_option").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"http://ha.local:8123/api/states/{switch_id}").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "entity_id": switch_id,
                    "state": "off",
                    "attributes": {},
                    "last_updated": "2026-08-08T10:00:00Z",
                },
            ),
            httpx.Response(
                200,
                json={
                    "entity_id": switch_id,
                    "state": "on",
                    "attributes": {},
                    "last_updated": "2026-08-08T10:00:01Z",
                },
            ),
        ]
    )
    respx.post("http://ha.local:8123/api/services/switch/turn_on").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with HaClient("http://ha.local:8123", "secret") as client:
        bad = await client.select_option(select_id, "Unknown")
        assert bad.status == AckStatus.MISMATCH
        ok = await client.select_option(
            select_id, "Command Charging (Grid First)", timeout_s=0.5, poll_interval_s=0.01
        )
        assert ok.status == AckStatus.ACKNOWLEDGED
        sw = await client.turn_switch(switch_id, True, timeout_s=0.5, poll_interval_s=0.01)
        assert sw.status == AckStatus.ACKNOWLEDGED


@respx.mock
async def test_ha_rejection_and_redaction() -> None:
    respx.post("http://ha.local:8123/api/services/switch/turn_on").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    async with HaClient("http://ha.local:8123", "super-secret-token") as client:
        with pytest.raises(HaError) as exc:
            await client.call_service("switch", "turn_on", {"entity_id": "switch.x"})
    assert "super-secret-token" not in str(exc.value)
    assert "401" in str(exc.value)


def test_redact_secrets_masks_bearer_token() -> None:
    assert "secret" not in redact_secrets("Authorization: Bearer secret-value")
    assert "***" in redact_secrets("Authorization: Bearer secret-value")


@respx.mock
async def test_control_snapshot_validates_bounds_and_options() -> None:
    select_id = "select.mode"
    number_id = "number.limit"
    respx.get(f"http://ha.local:8123/api/states/{select_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": select_id,
                "state": "Standby",
                "attributes": {"options": ["Standby", "Command Charging (Grid First)"]},
                "last_updated": "2026-08-08T10:00:00Z",
            },
        )
    )
    respx.get(f"http://ha.local:8123/api/states/{number_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": number_id,
                "state": "0.0",
                "attributes": {"min": 0, "max": 100, "step": 0.001},
                "last_updated": "2026-08-08T10:00:00Z",
            },
        )
    )
    async with HaClient("http://ha.local:8123", "secret") as client:
        snap = await client.get_control_snapshot(
            [
                ControlEntitySpec(select_id, "select"),
                ControlEntitySpec(number_id, "number"),
            ]
        )
    assert snap.validation_errors == []
    assert snap.states[number_id].as_float() == 0.0
