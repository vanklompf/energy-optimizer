# PvOpti live-control operator runbook

Status: documentation for eventual arming. **Not** authorization to actuate.
Ordinary dry-run operation does not require this runbook.

Related:

- [Sigenergy control contract](./sigenergy-control-contract.md)
- [Battery-control context](./battery-control-context.md)
- [Watchdog interface](./battery-control-watchdog-interface.md)
- [Deployment variables](./deployment-variables.md)
- [Implementation plan](./plans/2026-08-08_001527-pvopti-actual-energy-control.md)

## Authority boundaries

| Actor | May do | Must not do |
|---|---|---|
| Optimiser / planner | Publish recommendations; write plans | Command the inverter |
| Battery control plane | Lease + authorize + transactional HA writes when armed | Bypass ack/physical verification |
| HA watchdog automation | Restore local EMS on heartbeat loss | Enable Remote EMS |
| Operator | Arm/disarm via config; clear lockout after review | Fight manual HA changes |

PvOpti commands **battery** behavior only. Grid import/export is residual.

## Preflight checklist

1. Contract docs match installed entities/modes/timings.
2. `EO_MODE=dry_run` still default; confirm inventory not accidentally `control`.
3. Number-register acknowledgement proven reliable **or** leave `EO_BATTERY_CONTROL_NUMBER_REGISTER_ACK_RELIABLE=false` (blocks control).
4. Watchdog automation present, tested, and independent of the PvOpti process (HA-side). Host-loss bound still a release blocker if unproven.
5. Arm token empty until ready; required value is `pvopti-battery-control-armed`.
6. Export remains disabled until export stage passes.
7. Single replica / single SQLite volume for the site lease.

Exact entity map (defaults):

```text
switch.sigen_plant_remote_ems_controlled_by_home_assistant
select.sigen_plant_remote_ems_control_mode
number.sigen_plant_ess_max_charging_limit
number.sigen_plant_ess_max_discharging_limit
number.sigen_plant_ess_charge_cut_off_state_of_charge
number.sigen_plant_ess_discharge_cut_off_state_of_charge
```

Local restore values: **8.8 / 9.6 kW** and **100 / 0%**. Never copy HA displayed `0.0`.

## Arm / disarm / fallback / lockout

Until an authenticated mutation API exists, **do not** expose HTTP arm/clear.
Use configuration / redeploy:

**Arm (only after gates pass):**

1. Set `EO_BATTERY_CONTROL_NUMBER_REGISTER_ACK_RELIABLE=true` only if proven.
2. Populate watchdog entity IDs and enable HA watchdog automation.
3. Set `EO_BATTERY_CONTROL_ENABLED=true`.
4. Set `EO_BATTERY_CONTROL_ARM_TOKEN=pvopti-battery-control-armed`.
5. Set `EO_MODE=control`.
6. Redeploy; confirm `/api/status` → `battery_control.gates_ok` and `watchdog_healthy` semantics.
7. Confirm Remote EMS is off at idle; heartbeat non-retained on MQTT.

**Disarm / force fallback:**

1. Set `EO_MODE=dry_run` and/or `EO_BATTERY_CONTROL_ENABLED=false` and clear arm token.
2. Redeploy / restart.
3. Or call process shutdown (app runs verified fallback on stop when armed).
4. Or flip site emergency helper `input_boolean.pvopti_battery_control_emergency_off`.
5. Verify Remote EMS **off**, mode Standby/local, limits 8.8/9.6 and 100/0.

**Clear lockout:** only after root-cause review. Today: clear via DB/operator procedure after setting mode dry_run; do not auto-clear from the UI.

## Cadence and expiry

| Signal | Default |
|---|---|
| Control tick | `EO_BATTERY_CONTROL_CADENCE_SECONDS` (30) |
| Heartbeat publish | 15 s |
| Heartbeat expiry | 60 s |
| Plan max age | 900 s |
| Telemetry max age | 120 s |
| Physical verify | ≥ 15 s |
| Command lease TTL | heartbeat expiry |

## Diagnosis surfaces

- API: `GET /api/status` → `battery_control` (gates, lease, intent, blockers, last action, lockout).
- API: `GET /api/control/actions` recent audit (read-only).
- MQTT: `energy_optimizer/battery_control/heartbeat` (non-retained); `.../battery_control/state` sensors.
- Logs: container `energy-optimizer`; look for lease_conflict, blocked, fallback, UNACKNOWLEDGED_LIMIT.
- DB tables: `control_actions`, `controller_state`, `controller_lease`.

HTTP 200 from HA is **never** success by itself.

## Incident recovery

| Symptom | Action |
|---|---|
| Wrong power direction | Fail closed → fallback; investigate physical verify; do not re-arm until explained |
| Stuck Remote EMS on | Run fallback sequence; if HA down, use host-independent path if available |
| Stale plan/price/telemetry | Control blocks economic actions; restore data freshness |
| Duplicate controller / lease conflict | Stop extra replicas; one volume/owner; clear lockout after review |
| Heartbeat loss | Watchdog must restore local EMS and notify |
| DB corruption | Restore `/data` backup; start disarmed; reconcile Remote EMS off |

## Watchdog test (HA)

**Heartbeat-expiry path: passed under supervision on 2026-08-12.** Full evidence is
recorded in [the watchdog interface](./battery-control-watchdog-interface.md#attended-heartbeat-expiry-fallback-evidence).

The proved scenario was a genuine PvOpti heartbeat followed by deliberate HA ingestion
loss while Remote EMS had been supervisedly enabled in verified `Standby`. At 99.27 seconds
of stored-heartbeat age, HA latched one fallback and returned Remote EMS to off. The final
`Standby`, 8.8/9.6 kW, and 100/0% state was independently confirmed through raw function-03
register reads.

This does not authorize normal watchdog enablement or PvOpti arming. It also does not cover
HA-host loss, HA restart, Modbus/network loss, or the reliability of arbitrary number-register
writes. Keep both automations disabled outside separately authorized attended windows.

Physical validation of HA-host loss remains a separate authorized experiment.

## Rollback

1. Redeploy with the known-safe dry-run configuration.
2. Ensure env: `EO_MODE=dry_run`, control disabled, arm token empty.
3. Verify Remote EMS off and local limits restored.
4. Confirm `/api/status` shows `DRY_RUN` / disarmed and no lease held by a control owner.

## Residual risks

- HA host failure is not covered by an HA automation alone.
- Number-register read-back defect until acknowledgement is reliable.
- Discharge/export characterization incomplete → keep unauthorized.
- No authenticated HTTP arm/clear yet (intentional).
