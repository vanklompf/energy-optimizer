# Grid export 3b/3c — step to 6 kW and Pstryk sell — 2026-08-18

Status: **passed for 3b (PvOpti `EXPORT` stepped 2 → 4 → 6 kW
`Command Discharging (ESS First)`, last=`ok`, measured export 1.48 / 3.58 /
5.45–5.47 kW, never above the 6.0 kW cap); 3c partial — the 21:00Z hour
that contains 3a is settled but is not a tight match, and the 22:00Z hour
that contains 3b is not settled yet.** Not authorization for unattended
control. State A was restored; PvOpti is `dry_run`.

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

Pstryk billing frames are hourly. Latest refresh 22:14:38Z.

| UTC hour | Pstryk import kWh | Pstryk export kWh | Notes |
|---|---|---|---|
| 20:00–21:00 | 1.801 | 0.011 | before 3a (night noise / MSC) |
| 21:00–22:00 | 0.601 | **0.099** | contains 3a (~1.5 min at 0.36 kW ≈ 0.01 kWh on 1-min Sigen samples) |
| 22:00–23:00 | — | **not settled** | contains 3b; Sigen trapezoid 21:50–22:16Z ≈ **0.74 kWh** export |

The 21:00Z hour *did* settle a sell (0.099 vs 0.011 the hour before), so
Pstryk is registering export, but 0.099 kWh is larger than the ~0.01 kWh
3a pulse and Sigen 1-min telemetry in that hour also has unrelated spikes
(21:20Z, 21:49Z). That is not a unique 3a reconciliation.

The hour that should close 3c is **22:00–23:00Z** (~0.74 kWh Sigen-side).
Re-check `billing_meter` / `pstryk_meter_intervals` after that frame
appears (meter refresh every 15 min).

## What this releases

- 3b: ESS First battery export tracks 2 / 4 / 6 kW commands with last=`ok`
  and stays inside `EO_BATTERY_CONTROL_MAX_GRID_EXPORT_KW`.
- 3c: not closed. Pstryk is selling *something* in the 3a hour; wait for
  the 3b hour to settle before treating billed kWh as matched.

## What remains blocked

- Tight 3c match for the 22:00Z hour (pending settlement).
- 2d PV First vs ESS First with PV present.
- Unattended control; fallback-while-exporting.
