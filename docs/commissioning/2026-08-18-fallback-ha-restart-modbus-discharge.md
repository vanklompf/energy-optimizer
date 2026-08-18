# Fallback 4e/4f while discharging — 2026-08-18

Status: **passed for Home Assistant restart from a verified 1.0 kW
`Command Discharging (PV First)`; failed the 4f runbook pass (stale
command persisted on the inverter; watchdog did not restore A). Not
authorization for unattended control, charge-side 4e/4f, or Modbus-loss
containment.** State A was restored by attended writes after 4f; PvOpti
is `dry_run`.

## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`;
  PvOpti HEAD `084d8fe` deployed as `energy-optimizer:local`.
- Gates: `EO_MODE=control`, `EO_BATTERY_CONTROL_ENABLED`,
  `EO_BATTERY_CONTROL_AUTHORIZE_DISCHARGE`, `DISCHARGE` in supported
  directions. Grid-charge and export off.
- Active command: `POST /api/control/manual-command` `DISCHARGE` 1.0 kW
  against ~2.0 kW house load, PV 0. Heartbeat expiry 60 s. HA watchdog
  ingest/fallback automations on for the window; HA-start guard is
  `initial_state: true`.

## 4e — Home Assistant restart

- **A before:** raw Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%.
- **B (21:18:34Z):** raw Remote EMS `1`, mode `5`, limits 8.8/1.0 kW,
  cut-offs 100/2%; request `173f372e-8e14-43fc-ae1b-286844ee27e1`. PvOpti
  `last_action` reported `blocked watchdog_unhealthy` even though the
  watchdog later read ready and the inverter was discharging ~1 kW; **raw
  is the evidence**.
- **Fault:** `docker restart homeassistant` at **21:18:34Z**; container up
  **21:18:44Z**.
- **During HA down (21:18:56Z):** raw still Remote EMS `1`, mode `5`,
  1.0 kW. The last command persists while HA is unreachable. Expected.
- **Watchdog (21:19:07Z, ~33 s after restart, ~23 s after container up):**
  raw Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%; HA Remote EMS off,
  Standby, local Maximum Self Consumption. Emergency-off **on**, fallback
  latch **on**, watchdog-ready **off**. Limits were not restored to `0.0`.
  Remote EMS was not left on. `ha_start_guard` did the restore.
- Heartbeat-ingest and watchdog-fallback automations came back **off**
  (`initial_state: false` in YAML). They were turned on again at
  ~21:19:18Z for the rest of the window. Raw A still held.
- PvOpti after reconnect: `mode control`, actuation enabled, `LOCKOUT`,
  watchdog not ready. It did **not** resume commanding.

## 4f — Modbus path loss (HA host OUTPUT drop)

Latch and emergency-off cleared; 1.0 kW discharge re-armed
(`5d571261-7d5b-4924-ab86-844848412735`).

- **B (21:29:14Z):** last=`ok`; raw Remote EMS `1`, mode `5`, 8.8/1.0 kW,
  100/2%; HA battery **−0.997 kW**, Remote EMS on, PV First.
- **Fault (21:29:15Z–21:31:32Z):**
  `sudo iptables -I OUTPUT -d 192.168.0.172 -p tcp --dport 502 -j DROP`
  on jezyk (HA uses host network). Host raw reads blocked. Independent
  observer from the `energy-optimizer` bridge namespace (FORWARD, not
  dropped) still reached the inverter. Rule deleted in the script trap;
  confirmed gone afterwards.
- **During the 90 s blackout:** observer held **ems=1 mode=5 8.8/1.0 kW
  cut 100/2%** at every 15 s sample. HA Sigen entities **never went
  unavailable** — they stayed cached (`on` / PV First / 1.0 kW /
  battery −0.997). Watchdog-ready stayed on; emergency-off and fallback
  latch stayed off. Heartbeat kept flowing, so the 60 s timer path did
  not fire. `integration_state` never fired because the Remote EMS switch
  never passed through `unavailable`/`unknown`.
- **PvOpti:** T+15/T+30 last=`pending`; by T+45 (21:30:20Z) `LOCKOUT`
  `fallback_standby_unacknowledged`. It tried software fallback, polled
  the cached mode select, did not get a Standby ack, and lockouted. It
  did not keep issuing new discharge commands.
- **After path restore (21:31:43Z):** raw Remote EMS **still `1`**, mode
  **`1` (Standby)**, limits **8.8/1.0 kW**, cut-offs **100/2%**. A queued
  Standby write landed once Modbus returned; Remote EMS stayed on and
  the local limits/cut-offs were not restored. PvOpti remained LOCKOUT
  and did not resume. Watchdog still had not fired.
- **Attended restore (21:37:45Z):** disable actuation, then HA writes
  Remote EMS off, Standby, 8.8/9.6 kW, 100/0%. Raw A at **21:37:51Z**:
  Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%.

No inverter-native Modbus-loss timeout showed up in 90 s.

## What this releases

- **4e (discharge):** `ha_start_guard` restores full state A across an HA
  restart from a live 1.0 kW PV First discharge. PvOpti does not resume
  without clearing emergency-off / latch / lockout. Ingest and fallback
  automations must be turned back on after the restart; the start guard
  does not depend on them.
- **4g** is further closed for 4e on discharge.

## What remains blocked

- **4f** as specified: command fail-closed in software, but the inverter
  kept the last Remote EMS command for the whole outage, HA cached the
  old entity states so the watchdog never saw a fault, and A was not
  restored until an attended write. Unattended Modbus-loss containment
  is not proven. There is still no characterized inverter-native timeout.
- 4e/4f while **charging**.
- 4a/4b while discharging (heartbeat expiry / Sigen reload on discharge).
- Unattended control, export, 2d ESS First.
