# Grid-import charge 1c/1d — 2026-08-18

Status: **1c passed 2026-08-18 20:16–20:26Z for app-driven ~4 kW Grid First;
1d partial (physical plateau ~6.36 kW / import ~6.87 kW, last=`ok`, no
sawtooth; 8.8 kW not reached). Not authorization for unattended control,
discharge, or export.** State A was restored; PvOpti is `dry_run`.

Checkpoint 2 was not run: house load was ~0.27 kW, too close to the 0.12 kW
deadband for a meaningful discharge-to-load step.

## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`;
  PvOpti HEAD `b5570f7` deployed as `energy-optimizer:local`.
- Gates for the window: `EO_MODE=control`, `EO_BATTERY_CONTROL_ENABLED`,
  `EO_BATTERY_CONTROL_GRID_CHARGE_ENABLED`. Discharge and export left off.
- Command path: `POST /api/control/manual-command` (`direction=CHARGE`) then
  the 30 s / 0.5 kW ramp. Reason code `manual_grid_charge_test`.
- Control reserve on HpeNas: `energy_optimizer_battery_control_min_soc_pct: "2"`.

## A/B/A evidence

- **A before (20:15:09Z):** raw Remote EMS `0`, mode `1`, limits 8.8/9.6 kW,
  cut-offs 100/0%. Local Maximum Self Consumption, SoC 73.1%, battery −0.36 kW
  covering house load, PV 0, import ~0, export ~0. Watchdog healthy. All six
  control entities present. Actuation left off until the 4 kW request was
  armed, then enabled, then lockout cleared (the disable-on-deploy fallback
  had locked out with `actuation_disabled` because local MSC discharge sat
  above the 0.12 kW Standby band).
- **B 1c (20:16:28Z–20:26:42Z):** first verified step last=`ok` at 0.864 kW
  (Remote EMS `1`, mode `3` Grid First). Same-direction ramp 0.864 → 1.363 →
  2.86 → 3.859 → **4.0 kW** with last=`ok` every sampled cycle and
  `standby_physical_timeout` never appearing. Three hold samples at 4.0 kW:
  battery **3.999 / 4.0 / 4.001 kW**, import **4.563 / 4.500 / 4.441 kW**,
  export 0.000 kW, SoC 74.7–75.4%. Watchdog stayed healthy with Remote EMS on.
- **B 1d (20:26:51Z–20:36:32Z):** re-armed 8.8 kW without releasing Remote EMS.
  First step last=`ok` at **4.501 kW** (same-direction, no Standby). Climbed
  to commanded **6.861 kW**; battery power plateaued at **6.36 kW**, import
  **6.87 kW**, export 0. Further cycles stayed last=`ok` at 6.86 kW requested
  (ramp clamp is measured+0.5 kW, so a 6.36 kW physical hold cannot step
  toward 8.8). Import never approached `max_grid_import_kw` (11 kW). SoC
  75.8% → 80.6%.
- **A after (20:37:16Z):** raw Remote EMS `0`, mode `1`, limits 8.8/9.6 kW,
  cut-offs 100/0% reconfirmed. `POST /api/control/actuation {"enabled": false}`
  restored local limits. Redeployed `dry_run` / flags false.

## What this releases

- The same-direction ramp fix is live-proven: a live Grid First charge no
  longer aborts every 30 s waiting for Standby `|P|≤0.12 kW`.
- App-driven `Command Charging (Grid First)` at **~4 kW** tracks the charge
  limit and is covered by grid import with export ~0.
- `POST /api/control/manual-command` works as the charge path (alias of the
  old `manual-charge`).
- Clearing lockout no longer rewrote a live command to DISARMED in this
  window (lockout was cleared while DISARMED, before the first charge).

## What remains blocked

- Software-accepted **8.8 kW** grid-import charge. Physical charge plateaued
  at ~6.36 kW with import ~6.87 kW — consistent with the 6.0 kW Hybrid AC
  rating plus house load, not with a Standby sawtooth. 1d therefore does not
  pass as written.
- Discharge to house loads (checkpoint 2): not attempted; load was ~0.27 kW.
- Grid export (checkpoint 3) and unattended control.
- Disable-actuation fallback still lockouts when local MSC is already
  covering house load above the Standby band (`fallback_neutral_timeout` /
  `actuation_disabled`). Physically harmless here (Remote EMS was already
  off) but noisy.
