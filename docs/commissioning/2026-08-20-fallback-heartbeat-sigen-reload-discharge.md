# Fallback 4a/4b while discharging — 2026-08-20

Status: **passed for heartbeat expiry and Sigen integration reload from a
verified 1.0 kW `Command Discharging (PV First)`; not authorization for
unattended control or Modbus-loss containment.** State A was restored;
PvOpti is `dry_run`.

Charge-side 4a/4b already passed on 12/14 Aug. This window is the discharge
repeat enabled by ~2 kW added house load.

## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`
  (same as 18 Aug windows); PvOpti HEAD `0c45a3b` deployed as
  `energy-optimizer:local`.
- Gates: `EO_MODE=control`, `EO_BATTERY_CONTROL_ENABLED`,
  `EO_BATTERY_CONTROL_AUTHORIZE_DISCHARGE`, `DISCHARGE` in supported
  directions. Grid-charge and export off.
- Active command: `POST /api/control/manual-command` `DISCHARGE` 1.0 kW,
  900 s, request `f5c458fb-6dd8-4c0b-8641-d9329a913760`, reason
  `manual_discharge_test`. Night, PV 0, added load ~1.85–1.95 kW, SoC ~85%.
  Heartbeat expiry 60 s. HA watchdog ingest/fallback automations on;
  HA-start guard is `initial_state: true`.

## 4a — heartbeat expiry (ingest silence, process still running)

- **A before:** raw Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%.
- **B (23:17:34Z last=`ok` `8bf1d326…`):** raw Remote EMS `1`, mode `5`
  (PV First), limits 8.8/1.0 kW, cut-offs 100/2%; battery **0.997 kW**
  discharge, import **1.049 kW**, export 0, load 1.855 kW, SoC 85.6%.
- **Fault:** `automation.turn_off` on
  `automation.pvopti_battery_control_heartbeat_ingest` at **23:19:16Z**.
  PvOpti stayed up and kept the live command; HA stopped updating
  `input_datetime.pvopti_battery_control_last_heartbeat` (last valid
  **23:19:02Z** / `2026-08-20 01:19:02` Warsaw). Watchdog-ready went off
  immediately because ready requires ingest on. PvOpti then
  `blocked watchdog_unhealthy` and did not keep issuing new discharges.
- **Watchdog (23:20:06Z, ~50 s after ingest off, ~64 s after last
  heartbeat):** raw Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%. Latch
  **on**, emergency-off **off**, watchdog-ready **off**. Limits were not
  restored to `0.0`. Remote EMS was not left on. Ingest was turned back
  on and the latch cleared before 4b.

## 4b — Sigen integration reload

Same 1.0 kW discharge re-armed after 4a (`f0f108bf…` last=`ok` at
23:21:04Z).

- **B (23:21:16Z):** raw Remote EMS `1`, mode `5`, 8.8/1.0 kW, 100/2%;
  HA battery **−0.996 kW**, import 1.090 kW, export 0, load 1.949 kW.
- **Fault:** `homeassistant.reload_config_entry` on
  `switch.sigen_plant_remote_ems_controlled_by_home_assistant` at
  **23:21:26Z**.
- **Immediate (23:21:26Z):** raw Remote EMS already `0`, mode still `5`,
  limits still 8.8/1.0 kW, cut-offs 100/2%. HA Remote EMS **off**, latch
  **on**, emergency-off **on** (integration-state path drops ownership
  first, then waits for restore entities).
- **Watchdog (23:21:35Z, ~9 s after reload):** raw Remote EMS `0`, mode
  `1`, 8.8/9.6 kW, 100/0%; HA Remote EMS off, Standby. Emergency-off
  **on**, latch **on**, watchdog-ready **off**. Limits not `0.0`. Remote
  EMS not left on.
- PvOpti after reload: `mode control`, last=`blocked watchdog_unhealthy`,
  lockout inactive. It did **not** resume commanding. Ingest and fallback
  automations stayed on.

## What this releases

- HA watchdog restores full state A from a live **discharge** when the
  heartbeat ingest path goes silent, even if PvOpti is still running.
- Sigen config-entry reload from a live 1.0 kW PV First discharge follows
  the same OFF-first restore as 14 Aug charge-side 4b: emergency-off and
  latch both on, A restored, PvOpti does not resume without clearing
  them.
- 4g is closed for 4a–4e on both polarities.

## What remains blocked

- **4f** Modbus / network path loss — failed on discharge (18 Aug); do
  not re-run. Stage 3 residual.
- Unattended control.
