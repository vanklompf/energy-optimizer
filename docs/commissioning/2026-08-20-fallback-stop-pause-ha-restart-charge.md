# Fallback 4c/4d/4e while charging — 2026-08-20

Status: **passed for process stop, process hang, and Home Assistant restart
from a verified ~1 kW `Command Charging (Grid First)`; not authorization for
unattended control or Modbus-loss containment.** State A was restored; PvOpti
is `dry_run`.

Discharge-side 4c/4d/4e already passed on 18 Aug. This window is the charge-side
repeat.

## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`
  (same as 18 Aug windows); PvOpti HEAD `0c45a3b` deployed as
  `energy-optimizer:local`.
- Gates: `EO_MODE=control`, `EO_BATTERY_CONTROL_ENABLED`,
  `EO_BATTERY_CONTROL_GRID_CHARGE_ENABLED`, `CHARGE` in supported directions.
  Discharge and export off.
- Active command: `POST /api/control/manual-command` `CHARGE` 1.0 kW, 900 s,
  reason `manual_grid_charge_test`. Night, PV 0, load ~0.15–0.17 kW, SoC ~87%.
  Heartbeat expiry 60 s. HA watchdog automations on for the window; HA-start
  guard is `initial_state: true`.
- Charge ramp often verified at ~0.77 kW rather than 1.0 kW; that was enough
  for a live-charge fallback. After 4d unpause the inverter sat at a 1.0 kW
  limit.

## 4c — `docker stop energy-optimizer`

- **A before:** raw Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%.
- **B (22:45:45Z last=`ok` `0b098ee6…`, still last=`ok` at 22:48:15Z
  `91946b3c…`):** raw Remote EMS `1`, mode `3` (Grid First), charge limit
  **0.77 kW**; battery **0.773 kW**, import **1.077 kW**, export 0, load
  0.165 kW, SoC 87.0%.
- **Fault:** `docker stop` at **22:48:31Z** (exit 0, container gone
  **22:48:41Z**). Shutdown wrote Standby; raw sat **ems=1 mode=1 chg=1.0**
  until the watchdog.
- **Watchdog (22:49:32Z, ~51 s after stop complete, ~61 s after stop
  started):** raw Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%. Latch **on**,
  emergency-off **off**, watchdog-ready **off**. Limits were not restored to
  `0.0`. Remote EMS was not left on. `docker start` at 22:49:58Z.

## 4d — `docker pause energy-optimizer`

Latch cleared and 1.0 kW charge re-armed (`5301a63f-8e8b-4c4e-8290-498a69185377`)
before the pause.

- **B (22:52:29Z last=`ok` `b03bd047…`; still live at 22:52:59Z `777e6a21…`):**
  raw Remote EMS `1`, mode `3`, charge limit **0.766 kW**; battery **0.765 kW**,
  import **1.061 kW**, export 0, SoC 87.2%.
- **Fault:** `docker pause` at **22:53:08Z**.
- **Watchdog (22:54:11Z, ~63 s):** raw Remote EMS `0`, mode `1`, 8.8/9.6 kW,
  100/0%. Limits not `0.0`; Remote EMS not left on. Unpaused at the restore
  instant (22:54:11Z), so latch/ready were not snapshotted at T_watchdog; by
  22:54:59Z PvOpti had resumed charge and latch was off / ready on.
- Unpause left A only until the still-armed manual command re-wrote Grid First
  (`3b8f2f95…` last=`ok` at 22:54:42Z). That resumed charge became the 4e B.

## 4e — Home Assistant restart

- **B (22:55:12Z last_verified `3b8f2f95…`):** raw Remote EMS `1`, mode `3`,
  limits 1.0/9.6 kW, 100/0%. HA battery **0.999 kW**, import **1.322 kW**,
  PV 0, load ~0.15 kW, SoC 87.3%. Remote EMS on, Grid First. Ingest and
  fallback automations on.
- **Fault:** `docker restart homeassistant` at **22:55:22Z**; container up
  **22:55:33Z**.
- **During HA down (22:55:33Z and 22:55:44Z):** raw still Remote EMS `1`,
  mode `3`, 1.0 kW. The last command persists while HA is unreachable.
  Expected. PvOpti's 22:55:29Z cycle is `blocked watchdog_unhealthy`; **raw
  is the physical evidence**.
- **Watchdog (22:55:54Z, ~32 s after restart, ~21 s after container up):**
  raw Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%; HA Remote EMS off,
  Standby. Emergency-off **on**, fallback latch **on**, watchdog-ready **off**.
  Limits were not restored to `0.0`. Remote EMS was not left on.
  `ha_start_guard` did the restore.
- Heartbeat-ingest and watchdog-fallback automations came back **off**
  (`initial_state: false` in YAML). They were turned on again at ~22:58:42Z.
  Raw A still held. Start guard stayed on.
- PvOpti after reconnect: `mode control`, last=`blocked watchdog_unhealthy`,
  lockout inactive. Every cycle from 22:55:29Z through restore stayed blocked.
  It did **not** resume commanding.

## What this releases

- HA watchdog restores full state A from a live **grid-import charge** when
  PvOpti is stopped or paused. Heartbeat silence is enough; a paused process
  does not look alive.
- **4e (charge):** `ha_start_guard` restores full state A across an HA restart
  from a live ~1 kW Grid First charge. PvOpti does not resume without clearing
  emergency-off / latch. Ingest and fallback automations must be turned back
  on after the restart; the start guard does not depend on them.
- 4g is closed for 4c/4d/4e on both polarities.

## What remains blocked

- **4f** Modbus / network path loss — failed on discharge (18 Aug); do not
  re-run. Stage 3 residual.
- 4a/4b while discharging (heartbeat expiry / Sigen reload on discharge).
- Unattended control.
