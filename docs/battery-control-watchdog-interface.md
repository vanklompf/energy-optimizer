# Battery-control layered watchdog interface

Status: PvOpti application contract for an independent HA-side fallback actor.
This is **not** authorization to enable live Remote EMS control.

## Ownership

| Layer | Owner | Artifact |
|---|---|---|
| Heartbeat publish + expiry payload | PvOpti | MQTT `energy_optimizer/battery_control/heartbeat` (**non-retained**), DB `last_heartbeat_at` |
| Fake watchdog unit tests | PvOpti | `src/energy_optimizer/watchdog.py`, `tests/test_watchdog.py` |
| HA automation, emergency helper, notifications, HA startup reconcile | AnsibleNasConfigs / ansible-nas | site `homeassistant/` packages |
| Host-independent watchdog (or proven inverter timeout) | Separate safety workstream | Required before unattended live control |

An HA automation is independent of the PvOpti process but **not** independent of the HA host.
Until a host-loss bound is proven, `watchdog_healthy` remains false and live arming stays blocked.

## MQTT heartbeat contract

Topic: `{{ mqtt_node_id }}/battery_control/heartbeat` (default `energy_optimizer/battery_control/heartbeat`)

- QoS 1, **retain=false** always.
- Payload JSON example:

```json
{
  "ts": "2026-08-08T12:00:00+00:00",
  "expires_at": "2026-08-08T12:01:00+00:00",
  "state": "DISARMED",
  "owner_id": "<uuid>",
  "lockout": false
}
```

Rules:

1. Missing message, MQTT unavailable, or `now - ts > heartbeat_expiry_seconds` ⇒ unhealthy.
2. A retained stale "active"/"online" payload must never count as healthy.
3. Controller `LOCKOUT` or emergency helper ON ⇒ run fallback even if heartbeat is fresh.
4. Heartbeat must be published by a lightweight job independent of optimisation.

Default expiry: `battery_control_heartbeat_expiry_seconds` (60s). Publish interval:
`battery_control_heartbeat_interval_seconds` (15s).

## Required HA fallback sequence

On heartbeat loss, lockout, emergency helper, or HA startup with Remote EMS unexpectedly on:

1. `select.select_option` → `select.sigen_plant_remote_ems_control_mode` = `Standby`
   (or the configured `battery_control_fallback_mode`).
2. Restore **configured** local limits (never copy HA number states that show `0.0`):
   - `number.sigen_plant_ess_max_charging_limit` = **8.8**
   - `number.sigen_plant_ess_max_discharging_limit` = **9.6**
   - `number.sigen_plant_ess_charge_cut_off_state_of_charge` = **100**
   - `number.sigen_plant_ess_discharge_cut_off_state_of_charge` = **0**
3. `switch.turn_off` → `switch.sigen_plant_remote_ems_controlled_by_home_assistant`
4. Notify operators.

Hard rules:

- No watchdog path may `turn_on` Remote EMS.
- Do not auto-enable Remote EMS on HA or PvOpti restart.
- Manual emergency-off must work without a healthy PvOpti process or plan.

Suggested site helper (name may vary): `input_boolean.pvopti_battery_control_emergency_off`.

## Suggested HA automation triggers (site repo)

- **Periodic / timer (required):** `time_pattern` (e.g. every 15s) or an HA timer so
  heartbeat silence is evaluated after expiry even when MQTT becomes completely quiet.
  An MQTT-message-only trigger does **not** satisfy Stage C.
- Persist last valid heartbeat into an `input_datetime` (or equivalent) on ingest;
  the silence check reads that helper, not the current trigger payload.
- Reject retained, future, or malformed heartbeat payloads at ingest time.
- MQTT: optional immediate evaluation on heartbeat topic (in addition to the timer).
- State: controller lockout attribute / MQTT battery state `LOCKOUT`.
- State: emergency helper ON.
- Home Assistant start: if Remote EMS switch is `on`, run fallback once.

Keep the automation **disabled** (`initial_state: false`) until Task 11 physical validation and
arming gates are complete.

## Verification in PvOpti (non-actuating)

```bash
docker compose -f compose.dev.yml run --rm --no-deps app pytest tests/test_watchdog.py -v
```

These tests prove expiry, retained-payload rejection, configured 8.8/9.6 and 100/0 restores,
and that the fake watchdog never enables Remote EMS. They do not contact live HA.

## Remaining rollout blockers (outside this document)

- Prove HA-host loss recovery (inverter timeout or host-independent actor).
- Physical validation of process stop/hang, MQTT loss, integration reload, HA restart,
  network/Modbus loss at conservative limits.
- Reliable number-register acknowledgement before `mode=control`.
