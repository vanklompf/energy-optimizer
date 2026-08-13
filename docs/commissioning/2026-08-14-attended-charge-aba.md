# Attended Sigen charge A/B/A — 2026-08-14

Status: **passed for the exact attended charge-only path below; not authorization for unattended control, discharge, or export.**

## Proven scope

- Home Assistant: `2026.8.1`.
- Sigen config entry: loaded through its supported Options flow.
- Site overlay source commit: `fd238d2` (`feat(energy): enhance Energy Optimizer configuration with EV control features and watchdog automation`).
- Site overlay `modbus.py` SHA-256: `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`.
- PvOpti commit containing fresh number-register acknowledgement: `89bd1c0`.
- PvOpti tested tree at documentation time: `a1e2e73`.
- Command semantics: `Command Charging (Grid First)` with a 0.5 kW maximum charging limit.

The installed Sigen integration package version was not exposed by the read-only Home Assistant API. The overlay commit/hash and HA version above are therefore the reproducible local identity; do not infer a package version.

## A/B/A evidence

1. **A — baseline:** Remote EMS off, dormant mode `Standby`, maximum charge/discharge limits 8.8/9.6 kW, charge/discharge cut-offs 100/0%.
2. **B — bounded command:** Remote EMS enabled under supervision, mode `Command Charging (Grid First)`, maximum charge limit 0.5 kW.
3. Fresh Home Assistant state, independent raw function-03 register read-back, and physical battery/grid power agreed with the requested charging direction and limit.
4. **A — restoration:** `Standby`, 8.8/9.6 kW and 100/0% restored; Remote EMS off; control entities disabled; integration returned to read-only.

## What this releases

- Number-register acknowledgement may be treated as reliable only when the operator has confirmed the same HA/overlay behavior and exact command semantics.
- The evidence supports planning the next attended charge-only interval at 0.5 kW.

## What remains blocked

- Autonomous or unattended battery control.
- Discharge and battery export.
- Promotion of the live acknowledgement gate before the remaining fallback and host-loss gates are accepted.
- Assuming that the HA-hosted watchdog can recover from complete HA-host loss.

Related: [control runbook](../control-runbook.md), [watchdog evidence](../battery-control-watchdog-interface.md), and [Sigen control contract](../sigenergy-control-contract.md).
