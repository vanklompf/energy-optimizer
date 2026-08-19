# Grid export 3b/3c — step to 6 kW and Pstryk sell — 2026-08-18

Status: **passed for 3b (PvOpti `EXPORT` stepped 2 → 4 → 6 kW
`Command Discharging (ESS First)`, last=`ok`, measured export 1.48 / 3.58 /
5.45–5.47 kW, never above the 6.0 kW cap) and for 3c on the 22:00Z hour
(Pstryk settled sell **0.733 kWh** vs Sigen 1-min integral **0.732 kWh**).**
The 21:00Z hour that contains the short 3a pulse is settled but is not a
tight unique match. Not authorization for unattended control. State A was
restored; PvOpti is `dry_run`.

## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`;
  PvOpti HEAD `88da18f` deployed as `energy-optimizer:local`.
- Gates: `EO_MODE=control`, `EO_BATTERY_CONTROL_ENABLED`,
  `EO_BATTERY_CONTROL_AUTHORIZE_DISCHARGE`, `DISCHARGE` in supported
  directions, `EO_BATTERY_EXPORT_ENABLED`. Grid-charge off.
- Mode: `Command Discharging (ESS First)` (raw mode `6`). Night, PV 0,
  house load ~0.2–0.3 kW. SoC 71.9% → 67.5%.

## 3b — step toward `max_grid_export_kw` (6.0 kW)

- **A before (21:59:11Z / 22:00:28Z):** raw Remote EMS `0`, mode `1`,
  8.8/9.6 kW, 100/0%.
- **2.0 kW** (`35d1add7…`, armed 22:00:36Z). Hold 22:04:08Z: last=`ok`;
  raw Remote EMS `1`, mode `6`, limit **2.0 kW**, cut-offs 100/2%; battery
  **1.871 kW**, load 0.238 kW, import **0**, export **1.478 kW**.
- **4.0 kW** (`b399226e…`, armed 22:04:13Z). Hold 22:09:10Z: last=`ok`;
  raw limit **4.0 kW**; battery **3.992 kW**, load 0.182 kW, import **0**,
  export **3.582 kW**.
- **6.0 kW** (`336bb79a…`, armed 22:09:15Z). Hold 22:14:08Z: last=`ok`;
  raw limit **6.0 kW**; battery **5.988 kW**, load 0.24 kW, import **0**,
  export **5.452 kW** (HA 5.469 kW). 22:14:42Z last=`ok`, battery 5.998 kW,
  export **5.462 kW**, SoC 67.5%. Export stayed under 6.0 kW on every
  sample; no `site_export_exceeded`.
- **A after (22:15:25Z):** disable actuation plus attended HA writes; raw
  Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%.

The 6 kW command is the app/inverter export cap, not the 9.6 kW battery
discharge register cap. Battery tracked the commanded limit at each step
(unlike 1d's 8.8 kW charge plateau). Measured export is command minus load
minus ~0.15–0.5 kW (conversion / sample skew); the grid export sensor is
the evidence, and it never crossed 6.0 kW.

## 3c — Pstryk settled sell

Pstryk billing frames are hourly. Rechecked 2026-08-19 08:02Z (fetched_at).

| UTC hour | Pstryk import kWh | Pstryk export kWh | Sigen 1-min integral export kWh | Notes |
|---|---|---|---|---|
| 20:00–21:00 | 1.801 | 0.011 | — | night noise / MSC before 3a |
| 21:00–22:00 | 0.601 | 0.099 | 0.070 | contains 3a (~1.5 min at 0.36 kW); not a unique match |
| **22:00–23:00** | **0.011** | **0.733** | **0.732** | **3b step-up; billed sell matches Sigen to 0.001 kWh** |
| 23:00–00:00 | 0.000 | 0.006 | — | back to night noise after restore |

The 22:00Z hour is the 3c evidence: commanded battery export appears as
Pstryk settled sell, and the billed kWh matches the Sigen trapezoid over
the same hour (60 one-minute samples, 0.7319 kWh vs 0.733 kWh). Adjacent
hours return to ~0.006 kWh sell, so this is not a stuck meter offset.

The 21:00Z hour (3a) settled 0.099 kWh sell vs 0.011 the hour before, so
Pstryk registered *some* export, but Sigen only integrates 0.070 kWh and
that hour also has unrelated spikes. 3c is closed on the 3b hour, not on
the 3a pulse.

## What this releases

- 3b: ESS First battery export tracks 2 / 4 / 6 kW commands with last=`ok`
  and stays inside `EO_BATTERY_CONTROL_MAX_GRID_EXPORT_KW`.
- 3c: the 22:00Z commanded-export hour is billed. Pstryk sell 0.733 kWh
  matches Sigen 0.732 kWh. Checkpoint 3 is closed for night battery export.

## What remains blocked

- 2d PV First vs ESS First with PV present.
- Unattended control; fallback-while-exporting.
