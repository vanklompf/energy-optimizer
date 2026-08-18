# Grid export 3a — ESS First 0.8 kW from battery — 2026-08-18

Status: **passed for a PvOpti-driven `EXPORT` 0.8 kW
`Command Discharging (ESS First)` at night with PV 0; not authorization
for unattended control, 3b step-up, or 3c settled-sell reconciliation.**
State A was restored; PvOpti is `dry_run`.

## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`;
  PvOpti HEAD `8ca6dec` deployed as `energy-optimizer:local`.
- Gates: `EO_MODE=control`, `EO_BATTERY_CONTROL_ENABLED`,
  `EO_BATTERY_CONTROL_AUTHORIZE_DISCHARGE`, `DISCHARGE` in supported
  directions, `EO_BATTERY_EXPORT_ENABLED`. Grid-charge off.
- Active command: `POST /api/control/manual-command`
  `{"direction":"EXPORT","target_kw":0.8,"duration_seconds":900}`
  (`ec666e62-4bc2-4806-8cf4-deab4838e8c2`), reason `manual_export_test`.
  Default export mode `Command Discharging (ESS First)`.

The 2026-08-17 3a attempt was HA-direct `ESS First` at a 0.5 kW limit in
daylight and curtailed PV instead of exporting. This window is night, PV 0,
house load ~0.29 kW, so a 0.8 kW discharge is ~0.5 kW above load if the
inverter will push battery energy to the grid.

## A/B/A evidence

- **A before (21:50:35Z):** raw Remote EMS `0`, mode `1`, 8.8/9.6 kW,
  100/0%; local Maximum Self Consumption. SoC 72.3%, PV 0, load 0.278 kW,
  battery discharge 0.383 kW (MSC covering load), export 0.006 kW.
- **B (21:52:47Z last=`ok`; raw 21:52:59Z):** Remote EMS `1`, mode `6`
  (`Command Discharging (ESS First)`), limits 8.8/0.8 kW, cut-offs 100/2%.
  Battery **0.8 kW** discharge, load 0.294 kW, import **0**, export
  **0.357 kW**, PV 0, SoC 72.2%.
- **Hold:** 21:53:31Z last=`ok`; 21:53:43Z HA export **0.386 kW**, import 0,
  battery −0.798 kW, load 0.269 kW. 21:53:56Z raw still mode `6` / 0.8 kW;
  status export 0.357 kW, last=`ok`.
- **A after (21:54:22Z):** disable actuation plus attended HA writes; raw
  Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%.

Export stayed well above the 0.12 kW deadband and well under the 6.0 kW app
/ inverter export cap. Import stayed 0. Battery minus load does not exactly
equal measured export (~0.15 kW unaccounted; likely conversion loss and
sample skew) but the grid export sensor is unambiguously non-zero.

## What this releases

- This inverter **will export from the battery** under `ESS First` when the
  discharge limit is above house load and PV is 0. Checkpoint 3a is no longer
  blocked by the 17 Aug PV-curtailment result; that result was a daylight /
  limit-vs-PV interaction, not a hard "no battery export" rule.
- PvOpti's `EXPORT` path (not HA-direct) verified: last=`ok`,
  `ACTIVE_DISCHARGE`, mode register `6`.

## What remains blocked

- **3b** step toward `max_grid_export_kw` (6.0 kW).
- **3c** Pstryk settled sell for this interval (check after settlement).
- **2d** `PV First` vs `ESS First` with PV present (whether ESS First still
  curtails PV when the limit is high enough to export).
- Unattended control. Fallback-while-exporting.
