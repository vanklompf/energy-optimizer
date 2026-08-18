# Discharge to house loads 2a/2b/2c — 2026-08-18

Status: **2a, 2b, and 2c passed 2026-08-18 20:46–20:53Z for app-driven
`Command Discharging (PV First)` at 1.0 kW then 1.8 kW below ~2 kW house
load, export 0, cut-off register 2%. Not authorization for unattended
control, export, or 2d (ESS First).** State A was restored; PvOpti is
`dry_run`.

## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`;
  PvOpti HEAD `a674672` deployed as `energy-optimizer:local`.
- Gates for the window: `EO_MODE=control`, `EO_BATTERY_CONTROL_ENABLED`,
  `EO_BATTERY_CONTROL_AUTHORIZE_DISCHARGE`, `DISCHARGE` in supported
  directions. Grid-charge and export left off.
- Command path: `POST /api/control/manual-command` (`direction=DISCHARGE`).
  Reason code `manual_discharge_test`.
- Control reserve on HpeNas: `energy_optimizer_battery_control_min_soc_pct: "2"`.
- Added house load ~2.0 kW, PV 0.

## A/B/A evidence

- **A before (20:46:39Z):** raw Remote EMS `0`, mode `1`, limits 8.8/9.6 kW,
  cut-offs 100/0% after a hand restore. (The control-mode deploy had already
  started a planner discharge: Remote EMS off but dormant mode `5`, discharge
  limit 0.583 kW, cut-off 2%. Disable-actuation fallback did not return those
  dormant registers to A.) Local Maximum Self Consumption, SoC 80.4%, battery
  covering the ~2 kW load, export 0. Watchdog healthy.
- **B 2a (20:48:06Z–20:49:53Z):** Remote EMS `1`, mode `5`
  (`Command Discharging (PV First)`), discharge limit **1.0 kW**, cut-off
  **2.0%**. last=`ok` with reason `manual_discharge_test`. Three hold samples:
  battery **0.998 kW** discharge, load ~1.95–2.00 kW, import ~1.13–1.15 kW,
  export **0.000 kW**, SoC 80.2→80.0%.
- **B 2b (20:50:06Z–20:53:21Z):** same-direction ramp 1.0 → 1.498 → **1.8 kW**
  with last=`ok` and no Standby abort. Two hold samples: battery **1.798 kW**,
  load 1.98 kW, import 0.337 kW, export **0.000 kW**, SoC 79.6%. Cut-off still
  2.0%.
- **B 2c:** raw discharge cut-off register 40048 read **2.0%** throughout B,
  not 0%.
- **A after (20:54:14Z):** raw Remote EMS `0`, limits 8.8/9.6 kW, cut-offs
  100/0%; dormant mode was still `5` after software fallback and was written
  back to Standby (`1`) by hand. Redeployed `dry_run` / flags false.

## What this releases

- App-driven `Command Discharging (PV First)` below house load tracks the
  discharge limit, does not export, and writes the 2% commissioning reserve
  into the cut-off register.
- Same-direction discharge ramp (1.0 → 1.8 kW) does not wait for Standby idle.
- `POST /api/control/manual-command` with `direction=DISCHARGE` is live-proven.

## What remains blocked

- 2d: `Command Discharging (ESS First)` vs PV First, and any discharge with
  PV present.
- Checkpoint 3 grid export.
- Unattended control.
- Software fallback does not always restore dormant mode to Standby (`1`)
  after a live discharge; limits and Remote EMS off did restore. Hand-write
  Standby before treating A as complete.
