# Attended Sigen integration-reload fallback — 2026-08-14

Status: **passed for the exact attended charge-only fault below; not authorization for unattended control, discharge, or export.**

## Scope

- Start from independently verified A: Remote EMS off, Standby, 8.8/9.6 kW and 100/0%.
- Command only 0.5 kW `Command Charging (Grid First)`.
- Reload only the loaded Sigen Home Assistant config entry.
- Require the HA watchdog to fail closed and hold lockout despite fresh PvOpti heartbeats.

## Finding and correction

The first fault rehearsal exposed an entity-readiness race. On Sigen reload, the Remote EMS switch returned before the mode and number entities. The watchdog turned Remote EMS off, but attempted the remaining restores while their entities were unavailable. Independent cleanup restored A; the attempt was correctly recorded as failed.

The OFF-only recovery path was then changed and redeployed:

1. latch the fallback and emergency-off helper;
2. turn Remote EMS off immediately;
3. wait up to 30 seconds for every restore entity;
4. select Standby and restore 8.8/9.6 kW and 100/0%;
5. turn Remote EMS off again and notify.

No watchdog path enables Remote EMS. The emergency-off helper prevents a fresh heartbeat from clearing the fault before attended review.

## Passing evidence

- **A before:** raw Remote EMS 0, mode 1, 8.8/9.6 kW, 100/0%; local Maximum Self Consumption.
- **B:** raw Remote EMS 1, mode 3, 0.5/0.0 kW, 10.5/0% cut-offs; battery charging 0.485 kW, grid import 0.854 kW, grid export 0.000 kW.
- **After Sigen reload:** watchdog triggered at `2026-08-13T23:53:20.706613Z`; raw A was restored; physical state returned to local Maximum Self Consumption with battery discharge and negligible grid import; fallback latch and emergency-off were both on.
- **Final cleanup:** raw A independently reconfirmed; integration returned to `read_only=true`; all six control entities were disabled; both test automations and both latches/helpers were turned off.

## Remaining gates

- PvOpti process stop/hang.
- Home Assistant restart.
- Modbus/network-path loss.
- Complete HA-host-loss containment through an inverter-native timeout or independent actor.

Related: [control runbook](../control-runbook.md), [watchdog interface](../battery-control-watchdog-interface.md), and [charge A/B/A evidence](./2026-08-14-attended-charge-aba.md).
