# PvOpti roadmap to production

Goal: promote PvOpti from "always-planning, battery-actuation disarmed" to
**full live battery control** — grid-import arbitrage charging, discharge to
loads, and grid export — with a small, sane safety layer and no ceremony.

This replaces the previous roadmap, implementation plan, and commissioning
scripts (now in [`archive/`](./archive/)). Read [`DESIGN.md`](./DESIGN.md) first.

## Current state

- Deployed in `EO_MODE=dry_run`. The optimiser runs and publishes
  recommendations; no inverter writes happen.
- The full battery-control stack is implemented and tested (~226 tests) but gated
  off. EV relay control is implemented and opt-in.
- Attended 0.5 kW grid-charge was physically characterized (see
  `commissioning/`). Discharge and export have **never** been physically tested
  against this inverter.

## Assumptions (please review)

These drive the whole plan. Correct any that are wrong before Stage 0.

1. **Equipment safety is the inverter/BMS's job.** The Sigen inverter and battery
   BMS have independent protection (voltage/current/temperature/SoC). PvOpti
   commands cannot damage them under normal Remote-EMS operation. The worst
   app-level outcome is a suboptimal bill or extra cycling.
2. **Economic mistakes are acceptable.** We optimize for lower bills, not
   provable optimality; occasional bad decisions are fine.
3. **Grid export is permitted at this site** (DSO connection + metering allow
   prosumer export). Export is billed via Pstryk settled sell prices.
4. **Grid-import charging for arbitrage is desired** — charge cheap, discharge or
   self-consume when expensive. Import is billed via Pstryk settled buy prices.
5. **Single site, single operator.** No multi-tenant, no concurrent operators.
6. **Battery operating reserve is 15%** (`battery_soc_min_pct`), distinct from the
   BMS hard floor (`battery_hard_soc_min_pct`, default 0%). Confirm this value.
   (The old roadmap said 2%; code uses 15%. This resolves that contradiction.)
7. **EV and battery control should share one simple gating scheme:** an enable
   flag per actuator plus `EO_MODE`. No arm tokens, no evidence IDs.
8. **OIDC stays optional**; production runs behind the operator's reverse proxy.
9. **The OFF-only watchdog stays.** It is cheap and genuinely useful: if the app
   dies, the inverter returns to local self-consumption.
10. **Compose keeps `EO_MODE` overridable from `.env`** rather than being
    hard-forced to `dry_run`, so going live is a config change, not a code edit.

## Scale of the problem

Numbers that justify the Stage 0 passes below:

- 134 settings in [`config.py`](../src/energy_optimizer/config.py), of which
  **55 are `battery_control_*`**. Ten are dead in application code (see 0a).
- [`service.py`](../src/energy_optimizer/service.py) is ~1,965 lines — 21% of the
  9.4k-line package — and owns telemetry, prices, optimisation, EV control,
  battery control, fallback, reconnect, and MQTT.
- [`sigenergy_control.py`](../src/energy_optimizer/sigenergy_control.py) is ~838
  lines of transaction/verification machinery sized for the heavier safety model
  being walked back.
- ~6.8k lines of tests, with `test_battery_control_loop.py` (875) and
  `test_sigenergy_control.py` (615) encoding the current gate semantics.

Success criteria for Stage 0 overall: **no module over ~800 lines**, and **every
`EO_*` setting is read by application code**.

## Stage 0a — de-gate and delete dead config (next session, after review)

Docs-only work is already done. This is the first code change; review as a normal
PR. Keep the local gate green throughout: `make test`, `make lint`,
`make typecheck`, `make fe-build`.

Simplify the arming model in [`config.py`](../src/energy_optimizer/config.py):

- Remove `BATTERY_CONTROL_EXPECTED_ARM_TOKEN` /
  `BATTERY_CONTROL_EXPECTED_ACK_EVIDENCE_ID` and their validators in
  `_validate_battery_control_armed`.
- Remove the `battery_control_arm_token`,
  `battery_control_number_register_ack_reliable`, and
  `battery_control_number_register_ack_evidence_id` settings.
- Gate live battery actuation on exactly: `mode == "control"` **and**
  `battery_control_enabled`. Keep the real direction gates
  (`battery_control_grid_charge_enabled`, `battery_control_authorize_discharge`,
  `battery_export_enabled`) as simple on/off feature switches.
- Keep the SoC ordering, limit, and timing validators (they protect against
  nonsense config, not ceremony).

Delete these ten settings that no application code reads (they appear only in
`config.py`, tests, and `.env.example`), along with their validators and
`.env.example` entries:

| Setting | Note |
|---|---|
| `battery_control_authorize_export` | Looks like an export authorization gate but is never checked at runtime. The real gate is `battery_export_enabled` (`battery_control.py:262`). Delete it rather than wiring it up. |
| `battery_control_require_remote_ems_off` | Readiness gate, never consulted |
| `battery_control_mode_charge_pv_first` | Mode string no code path selects |
| `battery_control_mode_discharge_pv_first` | Mode string no code path selects |
| `battery_control_mode_discharge_ess_first` | Mode string no code path selects |
| `battery_control_mode_max_self_consumption` | Mode string no code path selects |
| `battery_control_min_dwell_seconds` | Validated, never wired |
| `battery_control_max_ramp_kw_per_s` | Validated, never wired |
| `battery_control_command_settle_seconds` | Validated, never wired |
| `battery_control_discharge_cutoff_margin_pct` | Validated, never wired |

Simplify runtime safety in
[`control_store.py`](../src/energy_optimizer/control_store.py) /
[`safety.py`](../src/energy_optimizer/safety.py):

- Replace persistent lockout-until-manual-DB-edit with an auto-recovering backoff
  (fallback to local control, retry after a cooldown), plus an explicit
  manual-clear control endpoint.
- Keep the independent `control_authorized` blockers for stale/missing inputs,
  lease ownership, watchdog health, and manual override.

Tests: **delete** tests that only assert the removed ceremony (arm token,
evidence ID, dead settings) rather than repairing them. Keep and adapt tests that
cover real behavior: fallback, verification, lease, stale-input blocking.

Update surrounding files: `compose.yml` / `compose.dev.yml` to read `EO_MODE`
from `.env` (default `dry_run`), `.env.example`, `AGENTS.md`, and `README.md`
(including removing the interim arm-token note) to match the simplified model.

## Stage 0b — de-clutter (behavior-preserving refactor)

Pure refactoring, no behavior change. Can land either before Stage 1 or after
go-live if you would rather commission first.

- Extract the battery-control loop out of
  [`service.py`](../src/energy_optimizer/service.py) into its own orchestrator
  module, leaving `service.py` as thin coordination.
- Simplify [`sigenergy_control.py`](../src/energy_optimizer/sigenergy_control.py)
  so its verification/transaction path matches the reduced safety model in
  [`DESIGN.md`](./DESIGN.md) — keep ordered restore and physical read-back, drop
  machinery that only existed to satisfy the removed gates.
- Split [`web/routes.py`](../src/energy_optimizer/web/routes.py) (~988 lines) by
  concern if it stays over the size target.

## Stage 0c — fix known gaps

- Daily report job in [`scheduler.py`](../src/energy_optimizer/scheduler.py)
  currently just re-runs `_optimise`; implement a real daily report or remove the
  job.
- The SPA badge in
  [`frontend/src/App.tsx`](../frontend/src/App.tsx) is hardcoded to `dry_run`;
  drive it from `/api/status`.
- Add authenticated control-mutation endpoints (enable/disable actuation, clear
  lockout) if live operation should be controllable from the UI.

## Stage 1 — attended commissioning at the site

Requires access to the live Home Assistant / Sigen instance (offered by the
operator). For each direction, issue an app-driven command and confirm read-back
plus physical response, then record a short note in `commissioning/`:

- Full-rate grid-import charge.
- Discharge to house loads.
- Grid export.
- Fallback: kill the app / drop HA and confirm the inverter returns to local
  self-consumption (watchdog path).

Discharge and export are new territory (never physically tested), so treat these
as the real checkpoints.

## Stage 2 — supervised live operation

Run `EO_MODE=control` with grid-charge, discharge, and export enabled for ~1
week, attended. Watch:

- Plan-vs-actual and settled Pstryk reconciliation in the Savings view.
- Control action audit log for stale-input blocking, restart/reconnect behavior,
  and fallback correctness.

## Stage 3 — steady state

Unattended live operation. Periodic review of settled-data savings; tune export
spread / grid-charge margin thresholds for real-world results.

## Known-missing functionality (tracked across stages)

- Real daily-report job (Stage 0c).
- Live-mode badge in the SPA (Stage 0c).
- Control-mutation API endpoints for enable/disable and lockout-clear (Stage 0c).
- Physically verified discharge and export (Stage 1).
