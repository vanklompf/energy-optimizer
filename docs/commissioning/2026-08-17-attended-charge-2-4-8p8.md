# Grid-import charge 1b/1c/1d — 2026-08-17

Status: **1b passed 2026-08-18 00:17–00:22Z for app-driven ~2 kW Grid First;
1c failed (sawtooth ramp); 1d not run. Not authorization for unattended
control.** State A was restored; PvOpti is `dry_run`.

## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`;
  PvOpti HEAD `3c5d824` plus uncommitted control-path fixes deployed as
  `energy-optimizer:local`.
- Gates for the window: `EO_MODE=control`, `EO_BATTERY_CONTROL_ENABLED`,
  `EO_BATTERY_CONTROL_GRID_CHARGE_ENABLED`. Discharge and export left off.
- Command path: `POST /api/control/manual-charge` then the 30 s / 0.5 kW ramp.

## A/B/A evidence

Earlier the same night (23:16–23:34Z) two 1b attempts failed: verification
rejected a stable 0.813 kW hold as stale, then a rebuild started already at
2 kW and `lockout/clear` DISARMed a live charge. Those are historical; the
passing 1b is below.

- **A before (00:17:03Z):** raw Remote EMS `0`, mode `1`, limits 8.8/9.6 kW,
  cut-offs 100/0%. Local Maximum Self Consumption, SoC 74.9%, battery covering
  house load. Watchdog healthy. Actuation left off until the 2 kW request was
  armed, then enabled, then lockout cleared.
- **B 1b (00:17:43Z–00:22:09Z):** first verified step last=`ok` at 0.8 kW;
  later last=`ok` at **1.803 kW**, import **2.116 kW**, export 0, SoC 75.1%,
  EMS Remote EMS. Raw at 1b accept: Remote EMS `1`, charge limit **2.0 kW**.
  Watchdog stayed healthy with Remote EMS on.
- **B 1c (00:22:17Z–00:28:11Z):** ramped through 2.0 → 2.5 → 2.99 → **3.489 kW**
  (import 3.865 kW, export 0). Every later 30 s cycle required Standby
  `|battery|≤0.12 kW` while already charging, hit `standby_physical_timeout`,
  fell back to local MSC, then re-ramped from ~0.8 kW. Timed out at 3.489 kW
  with last=`pending` (need ≥3.5 kW and last=`ok`).
- **A after (00:28:55Z):** raw Remote EMS `0`, mode `1`, limits 8.8/9.6 kW,
  cut-offs 100/0%. Local Maximum Self Consumption, SoC 75.9%. Redeployed
  `dry_run` / flags false.

## What this releases

- App-driven `Command Charging (Grid First)` at **~2 kW** tracks the charge
  limit and is covered by grid import with export ~0. Watchdog-ready may stay
  on while Remote EMS is on.
- Manual charge must be armed **before** actuation is enabled after a
  control-mode deploy; overnight the planner will otherwise start its own
  grid-charge ramp.

## What remains blocked

- 1c ~4 kW and 1d 8.8 kW as software-accepted steps. Same-direction ramps
  must not wait for Standby idle (fix is in the tree, not live-proven).
- Unattended control, discharge, and export.
- `clear_lockout` must not rewrite `ACTIVE_CHARGE` to `DISARMED` (fix in tree).


## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`;
  PvOpti HEAD `3c5d824` plus uncommitted control-path fixes deployed as
  `energy-optimizer:local`.
- Gates for the window: `EO_MODE=control`, `EO_BATTERY_CONTROL_ENABLED`,
  `EO_BATTERY_CONTROL_GRID_CHARGE_ENABLED`. Discharge and export left off.
- Command path: `POST /api/control/manual-charge` then the 30 s / 0.5 kW ramp.

## A/B/A evidence

- **A before (23:16:54Z):** raw Remote EMS `0`, mode `1` (Standby), limits
  8.8/9.6 kW, cut-offs 100/0%. Local Maximum Self Consumption, SoC 75.6%,
  battery −0.31 kW to house load, PV 0, import ~0, export ~0.006 kW. Watchdog
  healthy.
- **B1 (23:17:13Z–23:17:40Z), first 1b attempt:** Remote EMS on; charge limit
  0.813 kW; mode `Command Charging (Grid First)`; battery **+0.813 kW**, import
  **1.15–1.17 kW**, export 0. Software result `failed` /
  `fallback_neutral_timeout` (lockout). HA battery_power sat at 0.813 kW with
  `last_updated` 2 ms *before* `command_started`, so verification treated a
  stable commanded hold as `stale_pre_command:battery_power`. Fallback then
  required `|battery| ≤ 0.12 kW` within 15 s; Standby settled to −0.078 kW at
  ~15.7 s, just after the deadline.
- **A mid (23:18:38Z):** raw Remote EMS `0`, mode `1`, limits 8.8/9.6 kW,
  cut-offs 100/0% reconfirmed.
- **B2 (23:32:28Z), second 1b attempt:** started **out of A**. After an image
  rebuild the plant was already Remote EMS on, mode Standby, charge limit
  2.0 kW, battery **+2.001 kW**, import 2.377 kW. Clearing lockout dropped the
  controller to DISARMED; the planner’s discharge intent then failed
  `discharge_export_not_authorized` and fallback ran while the inverter was
  still charging. Lockout `fallback_unverified`.
- **A after (23:34:15Z):** raw Remote EMS `0`, mode `1`, limits 8.8/9.6 kW,
  cut-offs 100/0%. Local Maximum Self Consumption, SoC 75.7%, battery
  −0.37 kW. PvOpti redeployed `dry_run` / control flags false.

## What this releases

Nothing new for 1b/1c/1d. It does confirm:

- App-driven Grid First still produces the commanded charge current and
  covering import (0.81 kW and 2.0 kW observed).
- `watchdog_ready` no longer requires Remote EMS off (template deployed
  earlier in the window); it stayed healthy while Remote EMS was on.
- Disable-actuation fallback now runs while still live (API restore reached
  state A on the second attempt).

## What remains blocked

- Software-accepted 2 / 4 / 8.8 kW steps (1b/1c/1d).
- Unattended control, discharge, and export.
- Trusting a 15 s Standby-neutral wait after a non-zero charge.
- Starting a window from image rebuild without re-checking raw state A
  (B2 started already charging).

Fixes landed in the tree after B1 (2 s HA timestamp skew; default physical
verify timeout 30 s) but were not cleanly re-proven: B2 was invalid as an
A/B/A. Next 1b must start from independently verified A, arm manual charge
*before* clearing lockout, and not rebuild mid-step.
