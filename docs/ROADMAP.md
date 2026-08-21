# PvOpti roadmap to production

Goal: promote PvOpti from "always-planning, battery-actuation disarmed" to
**full live battery control** — grid-import arbitrage charging, discharge to
loads, and grid export — with a small, sane safety layer and no ceremony.

This replaces the previous roadmap, implementation plan, and commissioning
scripts (now in [`archive/`](./archive/)). Read [`DESIGN.md`](./DESIGN.md) first.

## Current state

- Stage 0 and Stage 1 are complete. Grid-import charge to 4 kW, PV First
  discharge to load, ESS First battery export at night, daylight PV First vs
  ESS First, and the fallback set on both polarities are physically
  characterized (see `commissioning/`). Two Stage 1 items are carried rather
  than closed: 1d full-rate import (plateaued at ~6.4 kW; waived) and 4f
  Modbus-loss containment (failed; do not re-run).
- **Stage 2 supervised live operation is the current stage.** Control runs
  continuously with grid charge, discharge, and export enabled; the operator
  reviews daily rather than attending each command. The operating document is
  [`OPERATIONS.md`](./OPERATIONS.md).
- HpeNas entered Stage 2 on 2026-08-22. The first autonomous interval selected
  discharge/export, completed with physical verification, and retained a healthy
  watchdog, active lease, and clear lockout state.
- Live battery actuation is gated on `mode == "control"` and
  `battery_control_enabled` only. Lockouts auto-expire after cooldown and can be
  cleared from the API. Compose reads `EO_MODE` from `.env` (default `dry_run`)
  so a fresh checkout is inert; the HpeNas inventory sets the live mode.
- EV relay control is implemented and opt-in.

## Assumptions

These drive the plan. Correct any that are wrong.

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
6. **Battery operating reserve** (`battery_soc_min_pct`), distinct from the
   BMS hard floor (`battery_hard_soc_min_pct`, default 0%). The code default is 15%, but
   the HpeNas site deploys a **2%** operating reserve as normal policy (relying on the BMS
   for equipment safety per assumption 1). The control-side reserve written to the inverter
   discharge cut-off (`battery_control_min_soc_pct`) matches that 2%. Do not raise it
   as a precondition for unattended operation.
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
  **55 are `battery_control_*`**. Nine are dead in application code (see 0a).
- [`service.py`](../src/energy_optimizer/service.py) is ~1,965 lines — 21% of the
  9.4k-line package — and owns telemetry, prices, optimisation, EV control,
  battery control, fallback, reconnect, and MQTT.
- [`sigenergy_control.py`](../src/energy_optimizer/sigenergy_control.py) is ~838
  lines of transaction/verification machinery sized for the heavier safety model
  being walked back.
- ~6.8k lines of tests, with `test_battery_control_loop.py` (875) and
  `test_sigenergy_control.py` (615) encoding the current gate semantics.

Success criteria for Stage 0 overall: **no module over ~800 lines**, and **every
`EO_*` setting is read by application code**. Stage 0 is done.

## Stage 0a — de-gate and delete dead config (done)

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

Delete these nine settings that no application code reads (they appear only in
`config.py`, tests, and `.env.example`), along with their validators and
`.env.example` entries. `battery_control_discharge_cutoff_margin_pct` was on this
list too, but Stage 1 wired it into discharge verification as the margin above the
SoC floor, so it stays:

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

Simplify runtime safety in
[`control_store.py`](../src/energy_optimizer/control_store.py) /
[`safety.py`](../src/energy_optimizer/safety.py):

- Replace persistent lockout-until-manual-DB-edit with an auto-recovering backoff
  (fallback to local control, retry after a cooldown), plus an explicit
  manual-clear control endpoint.
- Keep the independent `control_authorized` blockers for stale/missing inputs,
  lease ownership, and watchdog health. The operator override is stopping the
  container, not application state.

Tests: **delete** tests that only assert the removed ceremony (arm token,
evidence ID, dead settings) rather than repairing them. Keep and adapt tests that
cover real behavior: fallback, verification, lease, stale-input blocking.

Update surrounding files: `compose.yml` / `compose.dev.yml` to read `EO_MODE`
from `.env` (default `dry_run`), `.env.example`, `AGENTS.md`, and `README.md`
(including removing the interim arm-token note) to match the simplified model.

## Stage 0b — de-clutter (done)

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

## Stage 0c — known gaps (done)

- Daily report job in [`scheduler.py`](../src/energy_optimizer/scheduler.py)
  currently just re-runs `_optimise`; implement a real daily report or remove the
  job.
- The SPA badge in
  [`frontend/src/App.tsx`](../frontend/src/App.tsx) is hardcoded to `dry_run`;
  drive it from `/api/status`.
- Add the authenticated lockout-clear endpoint. Actuation remains deployment
  configuration; the operator override is `docker stop energy-optimizer`, not an
  in-process UI toggle.

## Stage 1 — attended commissioning at the site (done)

Finished 2026-08-20. Charge, discharge, export, and fallback on both polarities
are physically characterized; the evidence notes are in
[`commissioning/`](./commissioning/). The runbook is historical — current
operating policy is [`OPERATIONS.md`](./OPERATIONS.md).

Discharge and export were the new territory at the time. The code no longer
rejects non-charge commands with `command_mode_not_characterized`. It commands
discharge and export through configurable Sigenergy discharge modes, writes the
operating reserve to the inverter discharge cut-off register, and verifies each
direction against physical telemetry before treating the command as applied.

### Prerequisites cleared on 2026-08-17

Three defects made Stage 1 unrunnable. All three are fixed; none of them was a
missing feature, and each would have silently blocked or failed commissioning.

**`plan_not_ok` blocked every economic command, permanently.** `safety.py` warned
whenever `known_price_hours < horizon_hours`, any warning forces `LOW_CONFIDENCE`,
and `_control_blockers` then adds `plan_not_ok` to every economic action. The
database held zero `ok` runs in its entire history.

The comparison itself was the bug. Pstryk is the only price source, and
`_build_intervals` creates intervals only where a price row exists, so the effective
planning horizon has always been Pstryk's published coverage. `optimise_horizon_hours`
is merely an upper bound on the fetch window. The warning therefore compared real
coverage against a bound it could only equal in the rare case of a full window —
firing permanently, for a condition that is just the normal day-ahead cycle: roughly
10 h of forward coverage before the afternoon publication, up to ~34 h after it.

`safety.py` now warns only when coverage drops below `optimise_min_price_hours`
(8 h), which the publication cycle never does on its own and which therefore
indicates a genuine publication or fetch failure. This is safe because the acting
interval is gated separately and far more sharply, by `current_price_not_real`,
`current_price_unavailable`, and `current_price_stale`; a distant estimate can never
authorize a command in the present. The bound is back at 48 h so that no published
price is discarded, and `Run.horizon_hours` now records the horizon actually planned
rather than the configured bound.

Note `pad_prices` in `forecast/price.py` is dead code: it is exported and unit-tested
but never called, so nothing is padded today and the old warning text was misleading
on that point too. The optimiser already honours `price_is_real` if padding is ever
wired in — it refuses to let an estimated future justify grid charging now.

**A full battery blinded the optimiser.** HA stops emitting SoC updates when the
value is pinned, so a battery sitting at 100% tripped the 10-minute SoC staleness
threshold, and one stale entity flagged the whole snapshot — a hard `blockers`
entry, status `BLOCKED`, no plan at all. Measured across stored telemetry, 86% of
samples at exactly 100% SoC were flagged stale versus 7% below it. `_is_stale` now
accepts a pinned SoC when another Sigen entity reported inside the power-staleness
window, which proves integration liveness without trusting a genuinely dead feed.

**Command verification demanded a SoC tick it could never get.** Verification
required every signal, SoC included, to have updated after the command was issued,
within a 15-second deadline. The Sigen SoC sensor emits only on a 0.1% change —
roughly every 65 seconds at 1 kW — so the first discharge command would almost
certainly have been rejected as `stale_pre_command:soc` and fallen back, while the
inverter was in fact obeying. Fast signals (battery power, grid flows) still must be
post-command; SoC is now bounded by age instead
(`battery_control_max_soc_age_seconds`, 300 s), which suits its actual role as a
discharge-floor guard rather than a response signal.

No checkpoint waits for the planner. `POST /api/control/manual-command`
(`{"direction": "DISCHARGE", "target_kw": 2.0, "duration_seconds": 600}`) arms one attended
request that the control loop prefers over the plan interval until it expires. `direction`
is `CHARGE`, `DISCHARGE` (to house load), or `EXPORT` (to grid); `POST /api/control/manual-charge`
remains as a charge-only alias. Each request is capped by `battery_control_max_charge_kw`
(charge) or `battery_control_max_discharge_kw` (discharge/export), limited to 30 minutes,
dropped on restart, and refused at arm time unless its per-direction gate is on — grid charge
needs `EO_BATTERY_CONTROL_GRID_CHARGE_ENABLED`, discharge needs
`EO_BATTERY_CONTROL_AUTHORIZE_DISCHARGE` plus `DISCHARGE` in the supported directions, and
export needs `EO_BATTERY_EXPORT_ENABLED` on top. It changes *what* the loop commands, not
*whether* it may: gates, authorization blockers, the 0.5 kW/cycle ramp, and physical
verification all still apply. `DISCHARGE` declares no grid flow, so any measured export trips
`unplanned_export`; `EXPORT` declares the export flow so the export bounds are verified.

### Sub-tests

Status: **done** = evidence note exists; **partial** = narrower scope passed;
**open** = never physically attempted.

| # | Sub-test | Status |
|---|---|---|
| **1** | **Full-rate grid-import charge** | partial |
| 1a | 0.5 kW `Command Charging (Grid First)`, A/B/A | done — [2026-08-14](./commissioning/2026-08-14-attended-charge-aba.md) |
| 1b | Step to ~2 kW, verify limit tracking and import coverage | done — [2026-08-17/18](./commissioning/2026-08-17-attended-charge-2-4-8p8.md) |
| 1c | Step to ~4 kW | done — [2026-08-18](./commissioning/2026-08-18-attended-charge-4-8p8.md) |
| 1d | Step to site cap, verify against `max_grid_import_kw` | partial — same note (last=`ok` through 6.86 kW command; battery plateaued at 6.36 kW / import 6.87 kW, never reached 8.8 kW) |
| **2** | **Discharge to house loads** | done |
| 2a | Discharge below *net* load (load minus PV), zero export | done — [2026-08-18](./commissioning/2026-08-18-attended-discharge-pv-first.md) (1.0 kW vs ~2 kW load, PV First) |
| 2b | Discharge at net load | done — same note (1.8 kW vs 1.98 kW load; left 0.18 kW import rather than sitting on the export deadband) |
| 2c | Discharge cut-off register holds the configured control reserve (`EO_BATTERY_CONTROL_MIN_SOC_PCT`, relaxed to 2% for commissioning), raw read | done — same note (raw 2.0%) |
| 2d | Compare `Command Discharging (PV First)` vs `(ESS First)` with PV present — 3a night export passed on `ESS First` from battery; the 17 Aug daylight result still shows `ESS First` at a 0.5 kW limit curtailing PV, so this comparison must settle whether a higher limit exports PV surplus | done — [2026-08-19](./commissioning/2026-08-19-attended-2d-pv-first-vs-ess-first.md) (ESS First curtails PV at 0.5 kW *and* 4.0 kW; PV First 4.0 kW keeps PV on and exports PV + battery, 5.12–5.22 kW) |
| **3** | **Grid export** | done (night battery export ESS First; daylight PV-surplus needs PV First per 2d) |
| 3a | ~0.5 kW deliberate export | done — [2026-08-18](./commissioning/2026-08-18-attended-export-ess-first-0p8.md) (PvOpti `EXPORT` 0.8 kW `ESS First` at night, PV 0: battery 0.8 kW, export 0.36–0.39 kW, import 0). Prior [2026-08-17](./commissioning/2026-08-17-attended-export-ess-first-0p5.md) HA-direct 0.5 kW in daylight curtailed PV and does not contradict this. |
| 3b | Step toward `max_grid_export_kw`, respect site export limit | done — [2026-08-18](./commissioning/2026-08-18-attended-export-step-6kw.md) (2 / 4 / 6 kW `ESS First`, last=`ok`, export 1.48 / 3.58 / 5.45 kW, never above 6.0) |
| 3c | Reconcile exported energy against Pstryk settled sell data | done — same note (22:00Z hour: Pstryk 0.733 kWh sell vs Sigen 0.732 kWh; 23:00Z back to 0.006 kWh noise) |
| **4** | **Fallback** | partial |
| 4a | Heartbeat expiry | done — charge 2026-08-12; discharge [2026-08-20](./commissioning/2026-08-20-fallback-heartbeat-sigen-reload-discharge.md) |
| 4b | Sigen integration reload | done — charge [2026-08-14](./commissioning/2026-08-14-sigen-integration-reload-fallback.md); discharge [2026-08-20](./commissioning/2026-08-20-fallback-heartbeat-sigen-reload-discharge.md) |
| 4c | Process stop (`docker stop`) | done — discharge [2026-08-18](./commissioning/2026-08-18-fallback-stop-pause-discharge.md); charge [2026-08-20](./commissioning/2026-08-20-fallback-stop-pause-ha-restart-charge.md) |
| 4d | Process hang (`docker pause`) | done — same notes (both polarities) |
| 4e | Home Assistant restart | done — discharge [2026-08-18](./commissioning/2026-08-18-fallback-ha-restart-modbus-discharge.md); charge [2026-08-20](./commissioning/2026-08-20-fallback-stop-pause-ha-restart-charge.md) (`ha_start_guard` restored A, PvOpti did not resume) |
| 4f | Modbus / network path loss | failed — [2026-08-18](./commissioning/2026-08-18-fallback-ha-restart-modbus-discharge.md) (discharge: HA cached entities, watchdog never fired, inverter kept last Remote EMS command for 90 s; PvOpti lockouted; attended restore to A). Do not re-run. |
| 4g | Repeat 4a-4f while **discharging**, not just charging | partial — 4a–4e done on both polarities; 4f failed on discharge |

Sub-test 2a's ceiling is net load, not gross load: with PV producing, commanding
more than `load - pv` exports and correctly trips `unplanned_export`. Prefer a
window with little or no PV, or add a known resistive load, so cloud movement
cannot turn a passing test into a spurious fallback.

## Stage 2 — supervised live operation (current, short)

Run `EO_MODE=control` continuously with grid charge, discharge, and export
enabled. Review daily until the data looks boring, then drop to a weekly
review — that drop *is* Stage 3. Gate values, abort, residual risks, and what
to look at are in [`OPERATIONS.md`](./OPERATIONS.md). Do not invent extra
ceremony between here and unattended operation.

Three defects that only bite under continuous autonomous operation were fixed
before entry, since none of them could have surfaced in a short attended window:

**Daylight export would have thrown away PV.** The export command mode was a
static setting pinned to `ESS First`, which 2d proved curtails PV to zero at any
discharge limit. Under the planner that would have sold stored energy while
discarding live generation, and corrupted the very savings data Stage 2 exists to
collect. `sigenergy_control.py` now chooses `PV First` when measured PV exceeds
`battery_control_export_pv_threshold_kw` and keeps `ESS First` otherwise, which
is also the safe default when the PV sensor cannot be read.

**A pinned SoC failed command verification.** The 2026-08-17 fix taught the
planner snapshot to accept a pinned SoC, but verification kept a flat age check
and rejected it as `stale_soc` — observed on 2026-08-19 failing every cycle of an
export at 100% SoC, then falling back into lockout. Verification already proves
the integration is live by requiring post-command battery power, so age only
costs confidence in the cut-off the SoC guards. An over-age reading is now
accepted while it sits at least `battery_control_stale_soc_headroom_pct` clear of
that cut-off.

**Stored energy was worth nothing at the horizon edge.** See the terminal-value
entry below; it is fixed rather than deferred, because the bias would have
skewed a week of savings measurement.

## Stage 3 — unattended live operation

Same configuration as Stage 2; the only change is the review cadence (weekly,
not daily). Move as soon as a few days of armed operation look right — see
[`OPERATIONS.md`](./OPERATIONS.md). Tuning of export spread and grid-charge
margin belongs here, once enough settled data describes a single unchanged
configuration.

## Known-missing functionality (tracked across stages)

- ~~Physically verified discharge and export (Stage 1).~~ Discharge-to-load
  (PV First), night battery export (ESS First), and daylight PV-surplus export
  (PV First only; ESS First curtails PV even at 4 kW) are characterized.
- ~~A manual command path for discharge and export.~~ Done:
  `POST /api/control/manual-command` arms a self-expiring `CHARGE`, `DISCHARGE`, or `EXPORT`
  request, so checkpoints 1, 2, and 3 can each be scheduled instead of waiting for the planner
  to independently choose the direction under test. It authorizes nothing; every gate and
  blocker still applies.
- ~~Terminal value for stored energy.~~ Fixed before Stage 2. `optimiser.py`
  derives the salvage price from the horizon's own prices when
  `terminal_soc_salvage_pln_kwh` is left at `0.0`: stored energy is worth the
  cheapest visible import, after discharge efficiency and degradation. Using the
  cheapest rather than an average keeps it a floor, so the dump bias is removed
  without inventing arbitrage the published prices do not support. Set the
  setting explicitly, or `terminal_soc_salvage_auto: false`, to override.
- Modbus/network-loss containment (4f) is unresolved and is **accepted** for
  unattended operation: the inverter holds a command PvOpti already verified,
  within limits the BMS enforces independently, and PvOpti itself fails closed.
  Closing it later needs either an inverter-side timeout or a watchdog that
  notices frozen-but-present HA entities. Do not re-run the fault; it is
  characterized. It is not a gate on going unattended.
- Padding is built but not wired in (`forecast/price.py`). Wiring it would stabilise
  the horizon across the publication boundary and push the terminal edge past the
  next real cycle.
