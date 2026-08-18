# Grid export (checkpoint 3) — 2026-08-17

Status: **failed for 0.5 kW `Command Discharging (ESS First)` as a grid-export
command; not authorization for unattended control, discharge-to-load, or
export.** Physical discharge at the commanded limit *did* occur, and state A
was restored.

Checkpoint 2 was not run first: at the window, PV was ~2.2 kW against ~0.3 kW
house load, so grid export was already ~1.9 kW. Discharge-to-load cannot keep
export under the 0.12 kW deadband in that physics. Checkpoint 1 is blocked at
SoC 100%. This window therefore used the only direction that could be attempted
in daylight.

## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`;
  PvOpti HEAD `3c5d824` (planner left in `dry_run`; this was an attended
  HA-direct command, same method as the 2026-08-14 0.5 kW charge A/B/A).
- Gates enabled: none in PvOpti. HA Sigen `read_only: false`; six control
  entities enabled for the window.
- Command mode: `Command Discharging (ESS First)` with a 0.5 kW maximum
  discharging limit and discharge cut-off 15%.

## A/B/A evidence

- **A before (08:41:22Z):** raw Remote EMS `0`, mode `1` (Standby), limits
  8.8/9.6 kW, cut-offs 100/0%. Physical: local `Maximum Self Consumption`,
  SoC 100%, battery −0.001 kW, PV 2.212 kW, load 0.367 kW, import 0.000 kW,
  export 1.904 kW.
- **B (08:41:37Z):** raw Remote EMS `1`, mode `6`, charge limit 8.8 kW,
  discharge limit 0.5 kW, cut-offs 100/15%. HA mode
  `Command Discharging (ESS First)`; plant EMS `Remote EMS`. Three samples
  at battery −0.495 kW, import 0.042 kW, export **0.000 kW**. PV dropped to
  **0.000 kW**. Load 0.286 kW. SoC 100%.
- **A after (08:41:47Z):** raw Remote EMS `0`, mode `1`, limits 8.8/9.6 kW,
  cut-offs 100/0% reconfirmed. HA Remote EMS off, dormant mode Standby, plant
  EMS `Maximum Self Consumption`. PV 2.133 kW and export 1.845 kW resumed.
  Battery −0.02 kW.

## What this releases

- `Command Discharging (ESS First)` is a real, register-`6` command on this
  inverter. A 0.5 kW discharge limit is acknowledged in HA and in raw function
  03, and battery power tracks it (−0.495 kW).
- Writing the operating reserve into the discharge cut-off register (15%)
  read back raw as 15%, not 0%.
- Ordered restore (Standby → 8.8/9.6 kW and 100/0% → Remote EMS off) returned
  the plant to local self-consumption, including PV production.

## What remains blocked

- **Grid export via this mode at 0.5 kW.** Export went from 1.9 kW to 0. The
  inverter curtailed PV instead of pushing surplus to the grid. Checkpoint 3
  therefore does not pass.
- Discharge to house loads with `Command Discharging (PV First)` (checkpoint 2).
  This window used ESS First, and PV First is still untested.
- Whether a *higher* ESS First discharge limit, or PV First, will export.
  Not tried in this window.
- Pstryk settled sell for the commanded interval (there was no commanded
  export to settle).
- App-driven PvOpti actuation (`EO_MODE=control`).
- Unattended control, full-rate charge, and fallback-while-discharging.
