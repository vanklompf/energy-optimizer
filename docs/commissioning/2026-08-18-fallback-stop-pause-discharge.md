# Fallback 4c/4d while discharging — 2026-08-18

Status: **passed for process stop and process hang from a verified 1.0 kW
`Command Discharging (PV First)`; not authorization for unattended control,
HA restart, Modbus loss, or charge-side 4c/4d.** State A was restored;
PvOpti is `dry_run`.

## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`;
  PvOpti HEAD `d95b378` deployed as `energy-optimizer:local`.
- Gates: `EO_MODE=control`, `EO_BATTERY_CONTROL_ENABLED`,
  `EO_BATTERY_CONTROL_AUTHORIZE_DISCHARGE`, `DISCHARGE` in supported
  directions. Grid-charge and export off.
- Active command: `POST /api/control/manual-command` `DISCHARGE` 1.0 kW
  against ~2.0 kW house load, PV 0. Heartbeat expiry 60 s. HA watchdog
  automations on.

## 4c — `docker stop energy-optimizer`

- **A before:** raw Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%.
- **B (20:59:40Z):** last=`ok`; raw Remote EMS `1`, mode `5`, limits 8.8/1.0 kW,
  cut-offs 100/2%; battery **1.0 kW** discharge, import 1.206 kW, export 0,
  load 2.033 kW, SoC 78.4%.
- **Fault:** `docker stop` at **20:59:50Z** (exit 137).
- **Watchdog (21:00:51Z, ~61 s):** raw Remote EMS `0`, mode `1`, 8.8/9.6 kW,
  100/0%; HA Remote EMS off, Standby, local Maximum Self Consumption.
  Fallback latch **on**, emergency-off **off**, watchdog-ready **off**.
  Limits were not restored to `0.0`. Remote EMS was not left on.

## 4d — `docker pause energy-optimizer`

Latch cleared and watchdog-ready restored before re-arming. Same 1.0 kW
discharge command.

- **B (21:06:44Z):** last=`ok`; raw Remote EMS `1`, mode `5`, 8.8/1.0 kW,
  100/2%; battery **0.998 kW**, import 1.192 kW, export 0, load 2.053 kW,
  SoC 77.4%.
- **Fault:** `docker pause` at **21:06:44Z**.
- **Watchdog (21:07:55Z, ~71 s):** raw Remote EMS `0`, mode `1`, 8.8/9.6 kW,
  100/0%; HA Remote EMS off, Standby, local MSC. Latch **on**, emergency-off
  **off**, watchdog-ready **off**. Limits not `0.0`; Remote EMS not left on.
- Unpaused at 21:08:16Z; disable-actuation left raw A in place (21:08:52Z).

## What this releases

- HA watchdog restores full state A from a live **discharge** when PvOpti
  is stopped or paused. Heartbeat silence is enough; a paused process does
  not look alive.
- 4g is partially closed for 4c and 4d (discharge). Charge-side 4c/4d remain
  open.

## What remains blocked

- 4c/4d while **charging**.
- 4e Home Assistant restart.
- 4f Modbus / network path loss.
- 4a/4b while discharging (heartbeat expiry / Sigen reload on discharge).
- Unattended control, export, 2d ESS First.
