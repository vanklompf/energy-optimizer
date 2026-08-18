# Stage 1 attended commissioning runbook

Status: **procedure only. Nothing below is authorization to run unattended.**

This runbook covers the four Stage 1 checkpoints in [`../ROADMAP.md`](../ROADMAP.md):
full-rate grid-import charge, discharge to house loads, grid export, and fallback.
Every step is attended: an operator is at the site, watching physical telemetry, able
to pull Remote EMS authority by hand at any moment.

Read [`../DESIGN.md`](../DESIGN.md) and
[`../sigenergy-control-contract.md`](../sigenergy-control-contract.md) first. The
contract is authoritative whenever it disagrees with this file.

Record one dated note per checkpoint in this directory. Existing notes
([charge A/B/A](./2026-08-14-attended-charge-aba.md),
[integration reload](./2026-08-14-sigen-integration-reload-fallback.md),
[export ESS First 0.5 kW](./2026-08-17-attended-export-ess-first-0p5.md),
[charge 2/4/8.8 kW](./2026-08-17-attended-charge-2-4-8p8.md),
[charge 4 kW / 1d plateau](./2026-08-18-attended-charge-4-8p8.md),
[discharge PV First 1.0/1.8 kW](./2026-08-18-attended-discharge-pv-first.md)) are the format
to follow; a template is at the end of this file.

## What changed for Stage 1

PvOpti previously refused every non-charge command with
`command_mode_not_characterized`. It can now command discharge and export, so the only
thing standing between the planner and the inverter is configuration:

| Gate | Setting | Ships as |
|---|---|---|
| Global actuation | `EO_MODE=control` **and** `EO_BATTERY_CONTROL_ENABLED` | `dry_run` / false |
| Grid-import charge | `EO_BATTERY_CONTROL_GRID_CHARGE_ENABLED` | false |
| Discharge | `EO_BATTERY_CONTROL_AUTHORIZE_DISCHARGE` **and** `DISCHARGE` in `EO_BATTERY_CONTROL_SUPPORTED_DIRECTIONS` | false / absent |
| Export | `EO_BATTERY_EXPORT_ENABLED` (on top of discharge) | false |

Discharge uses `EO_BATTERY_CONTROL_DISCHARGE_COMMAND_MODE`
(`Command Discharging (PV First)`); export uses
`EO_BATTERY_CONTROL_EXPORT_COMMAND_MODE` (`Command Discharging (ESS First)`). Neither
Sigenergy discharge mode has ever been physically exercised at this site, which is the
whole point of checkpoints 2 and 3.

During a discharge command PvOpti also writes the operating reserve
(`EO_BATTERY_CONTROL_MIN_SOC_PCT`) into the inverter's discharge cut-off register. This
defaults to 15% in the image but is **relaxed to 2% on HpeNas for the commissioning
windows** (`energy_optimizer_battery_control_min_soc_pct`), matching the 2% site operating
reserve so attended discharge/export tests have room; it is raised back before unattended
operation. Cut-off enforcement is itself uncharacterized, so software verification still
rejects any sample within `EO_BATTERY_CONTROL_DISCHARGE_CUTOFF_MARGIN_PCT` of the floor.

## Deployment

PvOpti runs as the `energy_optimizer` ansible-nas role on HpeNas, built locally from
this source tree into `energy-optimizer:local`. Never start a separate Compose stack
against the live site.

```bash
/mnt/nas/media/code/AnsibleNasConfigs/.cursor/skills/deploy-ansible-nas/deploy-ansible-nas.sh HpeNas energy_optimizer
```

Gate values live in `HpeNas/group_vars/nas/main.yml` as
`energy_optimizer_mode`, `energy_optimizer_battery_control_enabled`,
`energy_optimizer_battery_control_grid_charge_enabled`,
`energy_optimizer_battery_control_authorize_discharge`,
`energy_optimizer_battery_export_enabled`, and
`energy_optimizer_battery_control_supported_directions`. The role refuses to deploy
`control` mode without the enable flag and both watchdog entity IDs.

## Preconditions for every checkpoint

Confirm all of these before each window, not once per day.

1. **Independent baseline (state A).** Raw Modbus function-03 read shows Remote EMS
   enable `0`, mode `1` (Standby), limits 8.8/9.6 kW, cut-offs 100/0%. Physical state is
   local `Maximum Self Consumption`. Use `tools/sigen_raw_diagnostic.py` — it only reads.
2. **Sigen integration writable.** `read_only: false` via the HA integration options
   flow, with Remote EMS off while the option changes. All six control entities enabled
   in the entity registry and present in `/api/states`.
3. **Watchdog live.** Both PvOpti watchdog entities resolve, the HA watchdog automations
   are on, and `input_boolean.pvopti_battery_control_emergency_off` and the fallback
   latch are both off. `/api/status` must report `watchdog_healthy`.
4. **Headroom.** SoC gives the checkpoint room to run without crossing the control reserve
   (`EO_BATTERY_CONTROL_MIN_SOC_PCT`, relaxed to 2% for commissioning) or 98%.
5. **Plan status is `ok`.** `plan_not_ok` blocks every economic command. Since the
   `optimise_min_price_hours` fix (see [`../ROADMAP.md`](../ROADMAP.md) "Prerequisites cleared
   on 2026-08-17"), the plan reaches `ok` whenever Pstryk's published coverage clears the 8 h
   floor — which the normal day-ahead cycle almost always does, so the old "not before 14:00"
   guidance no longer holds. Confirm from `/api/status` (`last_run.status == "ok"`) rather than
   assuming a time of day; the acting interval is still gated separately and sharply by
   `current_price_*`, so a distant estimate can never authorize a command in the present.
6. **The direction under test is drivable.** Every checkpoint can be scheduled with the manual
   command path below, so you no longer have to wait for the planner to independently choose the
   direction. The *physics* must still suit the checkpoint: checkpoint 2 (discharge-to-load)
   needs no PV and a real house load so nothing is pushed to the grid; checkpoint 3 (export)
   needs confirmed DSO export permission and headroom. A manual request only selects the
   direction — it authorizes nothing, and every gate and blocker still applies.
7. **Abort path rehearsed.** You can turn
   `switch.sigen_plant_remote_ems_controlled_by_home_assistant` off by hand, and you know
   that doing so returns the inverter to local self-consumption.

## Abort at any point

Turn Remote EMS off in HA. Then set `input_boolean.pvopti_battery_control_emergency_off`
so a fresh heartbeat cannot clear the fault, disable actuation via
`POST /api/control/actuation {"enabled": false}`, and restore state A by hand before
recording the attempt as failed. A failed attempt still gets a note.

## Checkpoint 1 — full-rate grid-import charge

Only 0.5 kW has been characterized. Step up, do not jump to 8.8 kW.

Drive each step with one manual request rather than waiting for the planner:

```bash
curl -X POST http://<host>:8320/api/control/manual-command \
  -H 'content-type: application/json' \
  -d '{"direction": "CHARGE", "target_kw": 2.0, "duration_seconds": 600}'
curl -X DELETE http://<host>:8320/api/control/manual-command   # release early
```

`POST /api/control/manual-charge` remains as a charge-only alias. The request expires by
itself (30 minutes maximum), is dropped on restart, and is refused at arm time unless the
grid-charge gate is on. It selects the direction; it authorizes nothing. Because
`clamp_intent_power` allows only `battery_control_max_power_step_kw` (0.5 kW) more than
measured battery power per 30-second cycle, the commanded power ramps: allow roughly
2 minutes to reach 2 kW and 9 minutes to reach 8.8 kW, and size `duration_seconds` to cover
the ramp plus the hold. The armed request and its ramp estimate appear under
`battery_control.manual_command` in `/api/status`, and the resulting actions carry reason
code `manual_grid_charge_test` in the audit log — quote it in the note so the evidence is not
mistaken for planner-driven behaviour.

Note the same-direction ramp fix (a live charge no longer aborts every cycle waiting for
Standby-neutral) is committed but not yet live-proven, so sub-test 1c is the checkpoint that
confirms it: it passes only when a ramp holds above 3.5 kW with `last=ok` and
`standby_physical_timeout` never appears.

1. Enable charge gates only; leave discharge and export off. Redeploy.
2. Run intervals at roughly 2 kW, then 4 kW, then the site cap, each held long enough for
   three good verification samples.
3. At each step confirm from raw Modbus, not just HA: commanded charge limit matches,
   battery charge power tracks the limit, grid import covers charge plus house load, and
   grid export stays 0.000 kW.
4. Watch that grid import never exceeds `EO_BATTERY_CONTROL_MAX_GRID_IMPORT_KW` (11 kW).
   Charging alone cannot reach it: `battery_control_max_charge_kw` is 8.8 kW, so peak import
   is 8.8 kW plus house load. Sub-test 1d verifies limit tracking at full charge rate; the
   import cap stays untested by this checkpoint.
5. Release to state A and confirm local behaviour resumes.

**Pass:** every step verifies within the deadline, `charge_limit_mismatch` never appears,
and release restores A.

## Checkpoint 2 — discharge to house loads

This is new territory. Start below house load so nothing is pushed to the grid.

1. Turn on `authorize_discharge` and add `DISCHARGE` to the supported directions. Leave
   `battery_export_enabled` **off** — with export off, any measured export fails the
   command closed with `unplanned_export` and triggers fallback. That is the safety net
   for this checkpoint. Redeploy, then arm the direction (it is refused unless the gates
   above are set):

```bash
curl -X POST http://<host>:8320/api/control/manual-command \
  -H 'content-type: application/json' \
  -d '{"direction": "DISCHARGE", "target_kw": 1.0, "duration_seconds": 600}'
```

   Resulting actions carry reason code `manual_discharge_test` — quote it in the note.
2. Command roughly half of current house load. With PV producing, the ceiling is *net*
   load (`load - pv`), not gross: exceeding it exports and correctly trips
   `unplanned_export`. Cloud movement shifts that ceiling minute to minute, so prefer a
   window with no PV and add a known resistive load instead of chasing a narrow margin.
3. Confirm: EMS mode reads `Command Discharging (PV First)`, battery power is negative and
   at least half the commanded limit, grid export stays under the 0.12 kW deadband, grid
   import falls by roughly the discharged power.
4. Read the discharge cut-off register raw and confirm it holds the configured control
   reserve (`EO_BATTERY_CONTROL_MIN_SOC_PCT`, 2% for the commissioning windows), not 0%.
5. Step up to roughly house load. Do not exceed it in this checkpoint.
6. Release to state A.

**Pass:** discharge verifies at both levels, export never appears, the cut-off register
took the reserve, and release restores A.

Also record which mode you used. If `PV First` behaves unexpectedly with PV present,
retry with `Command Discharging (ESS First)` via
`energy_optimizer_battery_control_discharge_command_mode` and note the difference — that
comparison is the most valuable thing this checkpoint can produce.

## Checkpoint 3 — grid export

Do this only after checkpoint 2 passes, and only after confirming the DSO connection and
metering actually permit prosumer export.

1. Turn on `battery_export_enabled` (on top of the checkpoint 2 discharge gates). Redeploy,
   then arm the export direction:

```bash
curl -X POST http://<host>:8320/api/control/manual-command \
  -H 'content-type: application/json' \
  -d '{"direction": "EXPORT", "target_kw": 0.5, "duration_seconds": 600}'
```

   Resulting actions carry reason code `manual_export_test`. The 2026-08-17 attempt was
   HA-direct and never exercised PvOpti's control path; this is the first app-driven export.
2. Command a discharge that exceeds house load by a small margin, so export is roughly
   0.5 kW. Note the earlier failure: at a 0.5 kW `ESS First` discharge limit the inverter
   curtailed PV to 0 rather than exporting. Test the hypothesis that the discharge limit caps
   plant output — command a limit *above* house load plus the desired export, and compare
   `PV First` against `ESS First` via `energy_optimizer_battery_control_discharge_command_mode`.
3. Confirm: EMS mode reads `Command Discharging (ESS First)`, export is measured and
   within the intent bounds, grid import is under the deadband, and export never exceeds
   `EO_BATTERY_CONTROL_MAX_GRID_EXPORT_KW` (6.0 kW, itself capped by the 6.0 kW inverter
   limit).
4. Step export up toward the cap, watching the site export limit.
5. Confirm the export shows up in Pstryk settled sell data for that interval. Settled
   values are the only accepted source of billed export.
6. Release to state A.

**Pass:** export verifies, stays inside both the app cap and the site limit, appears in
settled data, and release restores A.

## Checkpoint 4 — fallback

The heartbeat-expiry and Sigen-reload paths already passed
([2026-08-14](./2026-08-14-sigen-integration-reload-fallback.md)). The gaps are process
loss, HA restart, and Modbus/network loss — and none of them has been tested while
*discharging*.

Run each fault from an active, verified command. Repeat the set once while charging and
once while discharging, since the recovery direction differs.

| Fault | How | Expected |
|---|---|---|
| Process stop | `docker stop energy-optimizer` | Heartbeat expires, HA watchdog restores A within the expiry window |
| Process hang | `docker pause energy-optimizer` | Same as stop; a paused process must not look alive |
| HA restart | Restart Home Assistant | Inverter returns to local control; PvOpti does not resume commanding on reconnect without re-verifying |
| Modbus/network loss | Break the inverter network path | Command fails closed; no stale command persists |

For each: record the trigger timestamp, the observed watchdog action, the raw register
state afterwards, and whether the fallback latch and emergency-off were set. A fallback
that leaves Remote EMS on, or that restores limits to `0.0`, is a hard failure.

**Pass:** every fault returns the inverter to local self-consumption with state A
restored, from both charge and discharge, with no path that turns Remote EMS on.

## After every window

Restore `read_only: true`, disable the six control entities in the registry, set the
PvOpti gates back to their non-actuating values, redeploy, and confirm state A one last
time from raw Modbus.

## Note template

```markdown
# <checkpoint> — YYYY-MM-DD

Status: **passed|failed for <exact scope>; not authorization for unattended control.**

## Scope

- Versions: HA <version>, Sigen overlay <commit>, PvOpti <commit>.
- Gates enabled: <list>.
- Command modes: <mode strings>.

## A/B/A evidence

- **A before:** raw Remote EMS <n>, mode <n>, <limits>, <cut-offs>; <physical state>.
- **B:** <raw registers>; battery <kW>, grid import <kW>, grid export <kW>, SoC <pct>.
- **A after:** <raw registers reconfirmed>; local behaviour resumed.

## What this releases

<the narrow claim this evidence supports>

## What remains blocked

<everything else>
```
