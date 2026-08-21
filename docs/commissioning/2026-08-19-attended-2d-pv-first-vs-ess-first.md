# PV First vs ESS First with PV present (2d) — 2026-08-19

Status: **passed for the physics comparison: `Command Discharging (ESS First)`
curtails PV to 0 at both 0.5 kW and 4.0 kW limits; `Command Discharging (PV
First)` at 4.0 kW keeps PV producing and exports PV + battery. Not authorization
for unattended control.** State A was restored; PvOpti is `dry_run`.

Raising the ESS First discharge limit does **not** export PV surplus. Daylight
PV-surplus export needs PV First. Night battery-only export can stay ESS First.

## Scope

- Versions: HA `2026.8.1`; Sigen overlay commit `fd238d2`; overlay `modbus.py`
  SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`
  (same as 18 Aug windows); PvOpti HEAD `0c45a3b` deployed as
  `energy-optimizer:local`.
- Gates for the window: `EO_MODE=control`, `EO_BATTERY_CONTROL_ENABLED`,
  `EO_BATTERY_CONTROL_AUTHORIZE_DISCHARGE`, `DISCHARGE` in supported directions,
  `EO_BATTERY_EXPORT_ENABLED`. Grid-charge left off.
- Control reserve on HpeNas: `energy_optimizer_battery_control_min_soc_pct: "2"`.
- Daylight, PV ~1.3–1.6 kW, load ~0.22–0.30 kW, SoC 100% → 99.8%.

## A/B/A evidence

- **A before (08:20:43Z / 08:24:27Z):** raw Remote EMS `0`, mode `1`, limits
  8.8/9.6 kW, cut-offs 100/0%. Local Maximum Self Consumption. SoC 100%, PV
  ~1.3–1.5 kW, load ~0.23 kW, export ~1.1–1.2 kW, battery idle. Watchdog healthy.

- **B1 PvOpti `EXPORT` 4 kW, first ramp step 0.5 kW ESS First (08:24:57Z–08:28:34Z).**
  Request `943c94c3-9ede-4607-88ff-2a4714c345a8`, reason `manual_export_test`.
  Raw Remote EMS `1`, mode `6`, discharge limit **0.502 kW**, cut-off **2.0%**.
  HA: PV **0.000 kW**, battery **−0.498 kW**, load 0.29 kW, export **0.08 kW**,
  import 0. Same PV-curtailment as 17 Aug, now on the PvOpti path. Every cycle
  then failed verification `stale_soc` (~12.5 s): SoC had been pinned at 100%
  since 07:52Z, older than `battery_control_max_soc_age_seconds` (300 s).
  Fallback restored A (08:28:56Z raw Remote EMS `0`, mode `1`, 8.8/9.6, 100/0%);
  PV returned to 1.59 kW under local MSC.

- **B2 HA-direct ESS First 4.0 kW (08:29:42Z–08:31:13Z).** Written to tick SoC
  and test the "higher limit exports PV surplus" hypothesis. Raw Remote EMS `1`,
  mode `6`, discharge limit **4.0 kW**, cut-off 2.0%. Hold ~08:30:27Z and
  08:31:12Z: battery **−3.997 / −3.999 kW**, export **3.556 / 3.561 kW**, load
  ~0.22 kW, import 0, **PV 0.000 kW** (last_updated stuck at 08:29:45Z). Export
  is battery minus load minus ~0.2 kW, not PV surplus.

- **B3 HA-direct switch to PV First, same 4.0 kW limit (08:31:13Z–08:32:05Z).**
  Raw Remote EMS `1`, mode `5`. T+12 s (08:31:24Z): PV **1.435 kW**, battery
  **−3.997 kW**, load 0.223 kW, export **5.216 kW**
  (`1.435 + 3.997 − 0.223 ≈ 5.21`). Hold 08:32:04Z: PV **1.383 kW**, battery
  **−4.002 kW**, load 0.257 kW, export **5.122 kW**. Export stayed under the
  6.0 kW cap. SoC ticked **100.0 → 99.9%** at 08:32:04Z.

- **A after (08:32:12Z):** ordered restore (Standby → 8.8/9.6 kW and 100/0% →
  Remote EMS off). Raw Remote EMS `0`, mode `1`, 8.8/9.6 kW, 100/0%. Local
  Maximum Self Consumption; PV 2.75 kW and battery charging ~1.4 kW as SoC
  dropped below 100%. Redeployed `dry_run` / flags false (08:35Z). Final raw A
  reconfirmed 08:35:01Z.

- **PvOpti `EXPORT` 4 kW retry after SoC ticked (08:32:24Z, request
  `9c3c56c8-fcf4-437c-9414-9c08515aabe0`):** failed `fallback_neutral_timeout`
  (36 s). After B3, local MSC was charging the battery at **4.6 kW** from PV
  surplus; Remote EMS Standby never reached `|P| ≤ 0.12 kW`. Attended restore
  already had A; dry_run deploy cleared the lockout.

## What this releases

- **2d:** ESS First with PV present curtails PV at **0.5 kW and 4.0 kW**. The
  17 Aug 0.5 kW result was not a "limit too low" artefact. Daylight PV-surplus
  export cannot use ESS First.
- **PV First** at 4.0 kW with PV present keeps PV on, discharges the battery at
  the limit, and exports PV + battery − load, inside the 6.0 kW cap.
- Night ESS First battery export (3a/3b) is unchanged: that path is PV = 0.

## What remains blocked

- PvOpti `last=ok` for a daylight high-limit command: pinned-100% `stale_soc`
  in verification, then `fallback_neutral_timeout` when MSC is already charging
  well above the Standby band. The physics comparison did not need that ack.
- Using PV First as the live `EXPORT` command mode (today's default remains
  ESS First).
- Unattended control; charge-side fallback; 1d 8.8 kW charge; 4f Modbus-loss
  containment.
