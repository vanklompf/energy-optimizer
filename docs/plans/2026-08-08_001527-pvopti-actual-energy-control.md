# PvOpti Actual Energy Control Implementation Plan

Status: implementation handoff; supervised charge characterization complete; live rollout blocked.

**Audience:** A generic external coding harness or developer working from the PvOpti repository. No Hermes-specific tools, memories, skills, mounted paths, or deployment profiles are assumed.

**Goal:** Extend PvOpti from dry-run/recommendation operation to a fail-safe receding-horizon controller that can eventually command the Sigen battery through Home Assistant to minimize net electricity cost, while treating battery reserve, household demand, EV charging, departure targets, and explicit user overrides as constraints with higher priority than arbitrage profit.

**Architecture:** Keep the current forecasting and MILP planner as the source of desired energy flows. Add a separate control plane that translates only the current fresh plan interval into a typed intent, authorizes it through stricter control-specific safety checks, and applies it through a reversible Home Assistant/Sigenergy transaction adapter. Use two independent application gates, persistent lease/state/audit, physical verification, lockout, and a separately proven fallback/watchdog path.

**Tech stack:** Python 3.12+, Pydantic Settings, PuLP/HiGHS, SQLAlchemy/SQLite, APScheduler, HTTPX/Home Assistant REST, MQTT, FastAPI, React/TypeScript, pytest/respx, Docker Compose. Ansible and Home Assistant configuration live in separate repositories and are separate workstreams.

## Execution contract for the coding harness

Read these repository documents before editing:

1. `README.md` and `DESIGN.md` — current application architecture and commands.
2. `docs/battery-control-context.md` — site, policy, external-repository, and handoff facts not inferable from this repository.
3. `docs/sigenergy-control-contract.md` — empirical installed-system behavior; authoritative over upstream assumptions.
4. This plan — task order, test requirements, and rollout gates.

Rules:

- Ordinary implementation, tests, CI, review, and local Docker runs are **non-actuating**. Do not contact live Home Assistant, use live credentials, enable HA entities, change integration options, deploy infrastructure, or send physical control services.
- Keep `dry_run`, battery control disabled/disarmed, battery export disabled, and an empty arm token as defaults.
- Use fakes/emulators for HA writes and physical telemetry. No test may require live HA or an inverter.
- Run `git status --short` first. Preserve unrelated work; do not reset, clean, stash, or overwrite it. Stage exact paths only and inspect the staged diff before each commit.
- Work task-by-task with strict TDD: focused failing test, verify the expected failure, minimal implementation, focused pass, applicable broader suite/static checks, safety review, then a narrow commit.
- Docker-first verification is canonical: `make test`, `make lint`, `make typecheck`, and `make fe-build`. Focused tests run with `docker compose -f compose.dev.yml run --rm --no-deps app pytest <targets> -v`.
- Never equate HTTP 200 with command success. Unknown, stale, unavailable, malformed, contradictory, unacknowledged, or physically mismatched state fails closed.
- Implementation completion is not live-rollout approval. Discharge, export, failure injection, and unattended control remain blocked until their explicit empirical and watchdog gates pass.

## Current implementation starting point

- PvOpti characterization baseline: commit `933e26e`.
- The worktree may contain uncommitted optimizer/EV changes that overlap this plan. Inspect current symbols/tests and classify each task as complete, partial, or absent before editing; do not blindly replay stale steps.
- Task 1's empirical output is complete enough to begin non-actuating implementation. Remaining failure/discharge/export characterization is a rollout blocker, not a blocker for domain/configuration/shadow code.
- The installed integration has a critical number-register acknowledgement gap. Live authorization must remain impossible until Task 6/7 provides reliable acknowledgement or explicitly blocks with a stable reason code.

---

## 1. Scope, invariants, and terminology

### In scope

- Receding-horizon battery control from the first current interval of the existing PvOpti plan.
- Deliberate grid charging when a later avoided import or export opportunity has sufficient expected value.
- Deliberate battery-to-grid export when financially justified and all reserves/goals remain feasible.
- EV charging as an optimizer-owned load and goal, including departure minimum and one-shot 100% behavior already present in PvOpti.
- Explicit safe fallback to Sigen local/default EMS behavior.
- Command audit trail, status/API/MQTT/frontend observability, and staged deployment through the existing Ansible role.

### Not in the first live release

- Direct Modbus writes from PvOpti. Home Assistant remains the only actuation boundary.
- PV curtailment control.
- EV-to-grid/vehicle-to-home.
- Controlling arbitrary household loads.
- Multi-site or multi-inverter coordination.
- Automatic tariff/provider switching.
- Learning-based control. Forecasts and MILP remain deterministic and inspectable.

### Non-negotiable invariants

1. **Goals and safety outrank profit.** Hard battery limits, configured reserve, EV minimum-by-departure, export/import limits, device availability, and explicit user overrides cannot be violated to improve the objective.
2. **PvOpti does not directly command “grid import” or “grid export.”** It commands battery charge/discharge behavior. Grid flow is the residual of PV, load, EV load, and battery power; the controller must verify both inverter state and observed physical flow.
3. **No simultaneous charge/discharge or import/export arbitrage artifact.** The MILP and control translation must reject contradictory flows even if prices or numerical tolerances would make them look profitable.
4. **No control from stale or padded critical data.** A dry-run may still publish a low-confidence plan; live economic control requires a separate `control_authorized=True` decision and a fresh current price, telemetry, and current plan interval.
5. **No command without read-back.** An HTTP 200 from Home Assistant is not success. The requested entity states and physical power direction must be observed within bounded time.
6. **No indefinite remote command.** Before active rollout, an independently running watchdog must be proven to restore local/default EMS after PvOpti heartbeat loss. If the inverter/HA path cannot provide a bounded fail-safe for HA host failure or communications loss, active control remains blocked.
7. **Restart is safe.** Startup begins disarmed, reconciles actual inverter state, and either returns it to fallback or completes a fresh preflight before re-arming; it never replays a persisted old plan.
8. **Manual intervention wins.** A user changing remote EMS, mode, or limits outside the controller causes PvOpti to yield, disarm, and require an explicit re-arm rather than fighting the user.
9. **One owner only.** A persistent lease prevents two PvOpti processes from controlling the same inverter.
10. **Pstryk settlement remains accounting truth.** Settled Pstryk intervals are used for billed results/backtests; Sigen instantaneous telemetry is used for control feedback, never as a substitute for missing settled billing intervals.

## 2. Current codebase context

- `src/energy_optimizer/optimiser.py` already models PV/load/battery/grid flows and returns `PlanStepResult` values including `grid_to_battery_kwh`, `battery_to_grid_kwh`, and EV charging.
- `src/energy_optimizer/service.py` already orchestrates telemetry, forecasts, plan persistence, MQTT publishing, and verified EV relay control. Its EV failure behavior is the pattern to reuse, but battery control needs its own independent state machine and fallback semantics.
- `src/energy_optimizer/safety.py` currently decides whether a plan is blocked/low confidence/OK. Its `control_enabled` field must not be reused as sufficient authorization for physical battery control.
- `src/energy_optimizer/ha_client.py` reads telemetry and can call Home Assistant services, but has no typed/verified inverter control transaction.
- `src/energy_optimizer/scheduler.py` runs collection, optimization, and EV control jobs. Battery control must be a short, serialized job that consumes only a fresh persisted plan and never solves inside the command transaction.
- `src/energy_optimizer/store.py` persists telemetry, plans, and runs but not command intent, read-back, controller lease, or lockout state.
- `src/energy_optimizer/mqtt_publish.py`, `src/energy_optimizer/web/routes.py`, `frontend/src/api.ts`, and `frontend/src/views/NowView.tsx` provide the existing observability surface.
- Deployment is owned by external `ansible-nas` and `AnsibleNasConfigs` repositories. Their relevant paths and preserved characterization commits are documented in `docs/battery-control-context.md`; application code must not require those repositories for tests.
- Exact installed entity IDs, modes, ordering, timings, final safe state, and known read-back defects are documented in `docs/sigenergy-control-contract.md`. Do not rediscover or infer them during ordinary implementation.

## 3. Target runtime flow

Each control tick performs this sequence:

1. Acquire/renew the single-controller lease.
2. Read fresh HA telemetry and the configured control surface; classify missing/unavailable/unacknowledged values explicitly. Do not treat fallback number states as register acknowledgement.
3. Load the newest successful plan; select the interval containing `now`, not simply row zero.
4. Recompute current EV state and active user overrides; reject a plan whose assumptions are no longer true.
5. Translate the interval’s energy flows to a `BatteryControlIntent` with requested direction, target/max power, cut-off SoC, expiry, source run ID, and economic explanation.
6. Run control-specific authorization and dynamic clamps.
7. Apply hysteresis/deadband/minimum-dwell logic. A no-op still records a decision.
8. Execute an idempotent HA transaction using a safe transition sequence.
9. Read back control entities and physical telemetry until success or timeout.
10. Persist the requested command, observed result, verification metrics, and resulting controller state.
11. Publish heartbeat and status. The next tick re-plans/reconciles; no command is considered permanent.

State machine:

- `DISARMED`: remote EMS off; no writes except an explicit fallback/reconcile.
- `PREFLIGHT`: validate configuration, entity capabilities, freshness, lease, watchdog, and fallback.
- `ARMED_IDLE`: authorized to control, currently using neutral/fallback behavior.
- `ACTIVE_CHARGE`: verified charge command.
- `ACTIVE_DISCHARGE`: verified discharge command.
- `FALLBACK`: actively restoring local/default EMS after a soft failure.
- `LOCKOUT`: repeated mismatch, manual intervention, lease conflict, or unknown inverter state; requires explicit operator clear/re-arm.

## 4. Implementation tasks

### Task 0: Establish the actual code baseline without modifying it

**Objective:** Determine which plan requirements are already present in HEAD or the current worktree and preserve unrelated work.

**Steps:**

1. Run `git status --short`, `git log -5 --oneline`, and inspect diffs for every modified file.
2. Read the current config, optimizer, safety, HA client, service, scheduler, store, API, and relevant tests before assigning a task status.
3. Classify Tasks 2–15 as `complete`, `partial`, or `absent`, with exact symbols/tests as evidence.
4. Run the existing focused tests for code already present before changing them. Record failures as baseline failures; do not "fix" unrelated failures opportunistically.
5. Do not stage, reset, clean, stash, or commit pre-existing modifications during this audit.

**Acceptance:** A short task-coverage note exists in the harness work log. The worktree content and index are unchanged.

### Task 1: Consume the verified Sigenergy control contract

**Status:** Completed and preserved in commit `933e26e`.

**Objective:** Make empirical capability and unknown behavior explicit inputs to implementation. Do not repeat physical characterization during ordinary coding.

**Files:**
- Existing authoritative document: `docs/sigenergy-control-contract.md`
- Supporting non-repository context: `docs/battery-control-context.md`

**Required pre-implementation checks:**

1. Read both documents and encode only verified capabilities/options.
2. Preserve the tested sign convention, dormant-Standby ordering, ±0.12 kW neutral band, at-least-15-second physical verification deadline, explicit 8.8/9.6 kW local restore values, and 100/0% cut-offs.
3. Model the HA number-register read-back defect as an acknowledgement blocker; never infer an original value from displayed `0.0`.
4. Keep all uncharacterized modes, discharge/export behavior, autonomous expiry, and failure scenarios disabled or blocked by stable reason codes.
5. Do not modify the empirical document to make an implementation test pass. If a later design requires an unverified physical claim, record it as a rollout blocker and request a separately authorized maintenance-window experiment outside this coding task.

**Acceptance:** Tests and configuration names cite the contract's exact strings and timings. Unsupported capabilities cannot be authorized. No live service call is made.

### Task 2: Add explicit control configuration and startup validation

**Objective:** Introduce two-key activation, configurable entity mapping, safety limits, timings, and strict startup validation with safe defaults.

**Files:**
- Modify: `src/energy_optimizer/config.py`
- Modify: `.env.example`
- Modify: `tests/test_config.py`
- Modify: `README.md`

**Settings to add:**

- `mode: Literal["dry_run", "control"]`, retaining `dry_run` as default.
- `battery_control_enabled: bool = False` as a second independent gate.
- `battery_control_arm_token: str = ""` or equivalent explicit runtime arming value; control requires the documented expected token and must not infer arming from `mode` alone.
- Configurable entity IDs for remote switch, mode select, charge/discharge limit, charge/discharge cut-off SoC, and watchdog health/ack entities if used.
- Exact mode-option strings learned in Task 1.
- `battery_export_enabled: bool = False` separate from existing grid-charge permission.
- Site clamps: maximum grid import/export kW, maximum commanded battery charge/discharge kW, minimum/maximum control SoC, reserve SoC, charge/discharge cut-off SoC margins.
- Control cadence, maximum plan age, maximum telemetry age, command settle/poll/timeout, physical verification timeout, heartbeat interval/expiry, minimum dwell, deadband, maximum power step/ramp, retry limit, and lockout duration.
- Economic activation margin in PLN/kWh or PLN per interval above modeled degradation/fees.
- Configured fallback mode and `remote_ems_off` postcondition.
- Explicit local-restore values for this site: 8.8 kW charge, 9.6 kW discharge, 100% charge cut-off, and 0% discharge cut-off. These must never be populated from HA's displayed number states.
- Empirical Standby verification defaults: ±0.12 kW neutral band and physical timeout no shorter than 15 seconds.
- A capability/acknowledgement requirement that prevents control authorization while reliable number-register acknowledgement is unavailable.
- A supported-direction allowlist. Initial implementation may model discharge/export, but authorization remains false until their empirical rollout gates pass.

**TDD steps:**

1. Add failing tests asserting all defaults are non-actuating (`dry_run`, disabled, export disabled, empty arm token).
2. Add parameterized failing tests for missing entities, invalid option strings, invalid power/SoC relationships, nonpositive timings, export limit above inverter/site capability, control mode without both gates, and identical/unsafe command/fallback modes.
3. Run: `docker compose -f compose.dev.yml run --rm --no-deps app pytest tests/test_config.py -v`. Expected: new tests fail for the intended missing behavior.
4. Implement settings and a model validator that fails startup before scheduler creation.
5. Run the focused tests, then `make lint` and `make typecheck`.
6. Document every variable in `.env.example` without placing site secrets or an active arm token there.
7. Commit: `feat: add fail-safe battery control settings`.

### Task 3: Define typed control intents, decisions, and state transitions

**Objective:** Create a pure domain layer that can be exhaustively tested without Home Assistant.

**Files:**
- Create: `src/energy_optimizer/battery_control.py`
- Create: `tests/test_battery_control.py`

**Core types:**

- `ControlDirection`: `FALLBACK`, `IDLE`, `CHARGE`, `DISCHARGE`.
- `ControllerState`: states listed in Section 3.
- `BatteryControlIntent`: source run/interval, direction, requested power, cut-off SoC, expiry, grid-charge/export flags, expected grid direction/range, expected financial value, and reason codes.
- `ControlAuthorization`: allowed flag, blockers, warnings, clamped intent, and data timestamps.
- `ControlResult`: command ID, requested/observed state, entity read-back, physical verification, retries, latency, and failure/lockout reason.

**TDD cases:**

- Zero/near-zero flows map to `IDLE`.
- Grid-to-battery maps to charge only when grid charging is allowed.
- Battery-to-grid maps to discharge only when export is allowed.
- PV-only battery charging does not accidentally request forced grid charging.
- Battery-to-load discharge does not accidentally request forced export.
- Contradictory charge/discharge or import/export flows are rejected.
- Energy converts to kW using `dt_hours` and is clamped to configured power/ramp/site limits.
- Expired/non-current intervals are rejected.
- State transitions not in an explicit transition table are rejected.
- Direction reversal requires a neutral transition.

**Verification:** Run `docker compose -f compose.dev.yml run --rm --no-deps app pytest tests/test_battery_control.py -v`. The pure domain tests use no HA client.

**Commit:** `feat: model battery control intents and states`.

### Task 4: Make the optimizer’s first interval physically actionable

**Objective:** Ensure the MILP output cannot exploit flows the real inverter/controller cannot execute and that EV obligations remain feasible under control limits.

**Files:**
- Modify: `src/energy_optimizer/optimiser.py`
- Modify: `tests/test_optimiser.py`
- Modify: `src/energy_optimizer/explain.py`

**Changes:**

1. Add explicit permission constraints for grid charging and battery export independently.
2. Add or verify binary constraints preventing simultaneous battery charge/discharge.
3. Add a no-simultaneous-grid-import/export constraint or prove by formulation that every plan step satisfies it under all valid/negative price combinations.
4. Use site grid import/export limits in the MILP, not only battery power limits.
5. Keep battery degradation and round-trip losses in the objective; add the configured minimum economic activation margin so tiny/uncertain arbitrage does not cause real cycling.
6. Preserve hard EV energy-by-departure constraints. Opportunistic EV charging remains price/PV optimized, while guaranteed minimum and explicit one-shot charging may use grid according to the existing EV policy.
7. Add a reserve trajectory constraint for stationary battery energy committed to guaranteed EV charging and forecast uncertainty. The current operating reserve is 15%: ordinary household discharge, export, and opportunistic EV behavior preserve it; guaranteed departure charging may consume it. Opportunistic EV charging may bridge a live PV deficit from stationary storage only above reserve and only when conservative same-day surplus supports recovery, and it must not deliberately import grid energy.
8. Expose reason codes that distinguish `ev_guaranteed`, `ev_opportunistic`, `grid_charge_arbitrage`, `battery_export_arbitrage`, `self_consumption`, and `reserve_hold`.

**TDD scenarios:**

- Negative buy price may charge but never exceeds import, battery, or SoC limits.
- High sell price may export only with export permission and leaves reserve plus required EV energy feasible.
- A later EV departure target suppresses an otherwise profitable export.
- A one-shot 100% EV request receives priority over stationary-battery export.
- A low-value spread below degradation + activation margin produces no arbitrage.
- No time step contains simultaneous import/export or charge/discharge.
- Padded future prices cannot alone authorize the current live arbitrage step.

**Commands:**

- Failing/focused command: `docker compose -f compose.dev.yml run --rm --no-deps app pytest tests/test_optimiser.py -v`.
- Broader focused pass: use the same Docker command for `tests/test_optimiser.py tests/test_ev.py tests/test_ev_control.py -v`.

**Commit:** `feat: constrain optimizer output for live actuation`.

### Task 5: Separate planning safety from live-control authorization

**Objective:** Keep low-confidence recommendations visible while applying stricter, explicit rules to physical commands.

**Files:**
- Modify: `src/energy_optimizer/safety.py`
- Modify: `tests/test_safety_and_forecast.py`
- Modify: `src/energy_optimizer/service.py`

**Changes:**

- Retain plan statuses (`BLOCKED`, `LOW_CONFIDENCE`, `OK`) for recommendation output.
- Add control-specific inputs: current plan/run status and age, current interval bounds, source and age of current buy/sell price, telemetry ages per entity, inverter/control entity availability, EV telemetry freshness when an EV goal affects the step, lease status, watchdog health, manual override/reconciliation state, SoC/power limits, and recent command failures.
- Return `control_authorized` separately; never derive it from the existing `control_enabled` field name.
- Require status `OK` and all critical sources fresh for economic charge/export. Define a conservative fallback/idle path that does not require forecast confidence.
- Treat NaN/unknown/unavailable values as blockers, not zero.
- Record all blocker codes in stable machine-readable form.

**Tests:** Add a table-driven test for every blocker and boundary timestamp, including DST-aware interval selection. Verify that low-confidence planning remains publishable but cannot trigger live arbitrage.

**Commit:** `feat: authorize live control independently from planning`.

### Task 6: Implement HA control discovery and verified primitive operations

**Objective:** Extend the HA client with typed, testable operations for exact entities and service calls from the verified control contract.

**Files:**
- Modify: `src/energy_optimizer/ha_client.py`
- Modify: `tests/test_ha_client.py`

**Operations:**

- Fetch all control entity states/attributes in one snapshot.
- Validate select options and number bounds/steps at runtime.
- Set a number, select an option, and turn a switch on/off idempotently.
- Poll state read-back using monotonic deadlines.
- Return a typed acknowledgement result that distinguishes `ACKNOWLEDGED`, `UNACKNOWLEDGED`, `MISMATCH`, transport failure, HA service rejection, unavailable entity, value coercion/rounding, timeout, cancellation, and manual overwrite.
- Treat HTTP 200 plus unchanged/fallback `0.0` number state as `UNACKNOWLEDGED`, never success.
- Expose a capability flag for reliable number-register acknowledgement. The installed system currently lacks it; control authorization must block until a separately implemented/read-reviewed path supplies it.
- Redact HA tokens and sensitive headers in all logs/errors.

**TDD:** Use `respx` to cover successful writes, idempotent no-ops, option mismatch, HA 4xx/5xx, timeout, delayed read-back, unavailable states, rounded number read-back, fallback `0.0` after HTTP 200, unsupported acknowledgement, manual overwrite, and cancellation.

**Important:** These are primitives only. They must not decide mode ordering or arm the inverter.

**Commit:** `feat: add verified Home Assistant control primitives`.

### Task 7: Implement the Sigenergy transaction adapter

**Objective:** Convert an authorized intent into an ordered, idempotent, reversible inverter transaction.

**Files:**
- Create: `src/energy_optimizer/sigenergy_control.py`
- Create: `tests/test_sigenergy_control.py`

**Required behavior:**

1. Read current control/physical state before writing. If state is unknown or inconsistent, do not issue an economic command.
2. When Remote EMS is off, write and verify dormant `Standby` first. If the selector is unavailable or cannot acknowledge Standby, block/lock out.
3. Write conservative global limits/cut-offs from explicit configuration. Never derive originals from HA number states. If reliable acknowledgement is unavailable, record `UNACKNOWLEDGED_LIMIT` and do not continue to live authorization.
4. Enable Remote EMS into the already verified dormant Standby mode.
5. Wait for fresh physical battery power to enter ±0.12 kW within a deadline of at least 15 seconds.
6. Select only an empirically authorized command mode. Initially only the characterized charge path can pass this capability gate; discharge/export remain blocked.
7. Verify mode acknowledgement and physical direction/magnitude/SoC/grid bounds. Account for household load and PV when defining the expected grid range.
8. For direction changes, select Standby and verify the physical neutral band before applying the opposite direction.
9. On same-direction updates, change only acknowledged values outside tolerance and obey ramp/minimum-dwell limits.
10. On any partial failure, cancellation, mismatch, or manual overwrite, execute fallback and verify Remote EMS off; after repeated/unexplained failure persist `LOCKOUT` and stop retrying.
11. Fallback is plan-independent and idempotent: select Standby when possible, verify neutral, restore explicit 8.8/9.6 kW and 100/0% local values, turn Remote EMS off, then verify local `Maximum Self Consumption` physical behavior.

**TDD:** Script mocked HA state sequences for dormant Standby, characterized charge, blocked discharge/export, reversal, no-op, stale fallback number state, unacknowledged limit, partial write, wrong read-back, no physical response, manual override, fallback success/failure, and cancellation. Assert the exact service-call order and prove no enable/command call occurs after an acknowledgement blocker.

**Commit:** `feat: add transactional Sigenergy controller`.

### Task 8: Persist control audit, lease, and lockout state

**Objective:** Make every decision reconstructible and prevent split-brain control.

**Files:**
- Modify: `src/energy_optimizer/store.py`
- Create or modify: SQLAlchemy schema initialization/migration file used by this repository
- Create: `tests/test_control_store.py`

**Tables/data:**

- `control_actions`: immutable command ID, timestamps, source run/interval, intent JSON or normalized fields, authorization, blockers, requested state, observed state, physical metrics, result, and error code.
- `controller_state`: current state, armed/disarmed reason, consecutive failures, lockout time, last successful command, last fallback verification, manual override marker.
- `controller_lease`: target/site key, owner UUID, acquisition/renewal/expiry using an atomic compare-and-swap transaction.

**Rules:**

- Never store HA tokens or other secrets.
- Persist the intent before the first external write; update result after read-back.
- A stale plan/run ID can never be replayed after restart.
- Lease loss immediately triggers fallback if this process still has HA access.

**TDD:** Concurrent lease acquisition, lease renewal/expiry, crash-style pending action recovery, lockout persistence, and append-only audit behavior.

**Commit:** `feat: persist battery controller audit and lease`.

### Task 9: Integrate battery control into the service and scheduler

**Objective:** Run a serialized, bounded control loop without coupling command execution to solving.

**Files:**
- Modify: `src/energy_optimizer/service.py`
- Modify: `src/energy_optimizer/scheduler.py`
- Modify: `src/energy_optimizer/__main__.py` if startup/shutdown hooks live there
- Modify: `tests/test_service.py`
- Modify: `tests/test_scheduler.py`

**Service methods:**

- `build_battery_control_intent(now)` selects the containing fresh interval and revalidates EV/telemetry assumptions.
- `control_battery(now, force_fallback=False)` performs lease, authorization, transaction, audit, and status publication.
- `fallback_battery(reason)` is independent of optimizer availability.
- `reconcile_battery_on_startup()` never trusts persisted desired state.
- `shutdown_battery_control()` attempts verified fallback and lease release within a bounded timeout.

**Scheduler behavior:**

- Run optimization on its current cadence.
- Run control at a shorter configured cadence with `max_instances=1`, coalescing, and a hard timeout.
- Trigger one immediate control reconciliation after a successful fresh optimization.
- On collection, optimization, bootstrap, control, or unexpected scheduler failure, invoke battery fallback independently from EV `force_off` handling.
- Publish controller heartbeat from a lightweight independent job so slow optimization cannot masquerade as health.
- Prevent one control tick from overlapping the next.

**TDD:** Verify no command in dry-run, either missing gate blocks control, stale plan falls back, optimizer exception falls back, telemetry failure falls back, startup with remote EMS unexpectedly on falls back, shutdown falls back, lease conflict locks out, and EV control still follows its existing failure behavior.

**Commit:** `feat: run fail-safe receding-horizon battery control`.

### Task 10: Add operator API, MQTT, and frontend observability

**Objective:** Make live state, authority, intent, read-back, and failure obvious; never present “service call sent” as “control succeeded.”

**Files:**
- Modify: `src/energy_optimizer/mqtt_publish.py`
- Modify: `src/energy_optimizer/web/routes.py`
- Modify: `tests/test_mqtt_discovery.py`
- Modify: `tests/test_api.py`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/views/NowView.tsx`
- Add frontend tests if the repository’s frontend test harness exists; otherwise validate with typecheck/build.

**Status fields:**

- configured mode, both activation gates, effective armed state, controller state, lease owner/expiry health, watchdog health, current intent, requested and observed mode/power/SoC cut-offs, source plan/run/interval and age, last verified action, last fallback, lockout/blockers, command latency, and physical verification result.

**Endpoints/actions:**

- Read-only status and recent audit history.
- Explicit `arm`, `disarm/fallback`, and `clear-lockout` actions, protected by the application’s existing network/auth boundary plus a confirmation/nonce. If the app currently has no authenticated mutation API, do not expose arm/clear publicly; use configuration/CLI until authentication is designed.
- Disarm must be idempotent and always available even when planning data is broken.

**MQTT:** Publish heartbeat/availability and controller sensors with bounded payloads. Avoid retained “active” state that can outlive the controller; use expiry/availability semantics.

**UI:** Add a highly visible `DRY RUN`, `ARMED`, `CHARGING`, `DISCHARGING`, `FALLBACK`, or `LOCKOUT` badge. Show why, requested vs measured power, expiry, financial rationale, and unmet/at-risk EV goal. Require explicit confirmation for disarm/arm if mutation is exposed.

**Commands:** Run the focused API/MQTT tests through `compose.dev.yml`, then `make fe-build`.

**Commit:** `feat: expose verified battery control status`.

### Task 11: Add layered fallback watchdogs before live control

**Objective:** Restore local inverter control after PvOpti failure and explicitly cover—or block on—HA-host failure.

**Repository boundary:**

- PvOpti owns heartbeat publication, expiry semantics, controller lockout state, and test fixtures.
- The site configuration repository owns the HA automation, emergency helper, notifications, and startup reconciliation.
- A host-independent watchdog or inverter-enforced timeout is a separate safety layer. An HA automation is independent of PvOpti but is **not** independent of the HA host.
- If the external repositories are unavailable, document the required interface/artifacts and stop; do not create guessed deployment files inside PvOpti.

**Required behavior:**

- Publish a bounded, expiring PvOpti heartbeat/availability signal from a lightweight job independent of optimization.
- HA-side watchdog: on heartbeat loss, controller `LOCKOUT`, or emergency helper activation, execute the empirical fallback using explicit local restore values, turn Remote EMS off, and notify.
- HA startup reconciliation: if Remote EMS is unexpectedly on, fall back; never auto-enable it.
- Manual emergency-off remains available without a healthy PvOpti process or plan.
- Determine and implement the HA-host-loss outcome. If no inverter timeout or host-independent actor can restore Remote EMS off within a bound, active control remains permanently blocked.

**Non-actuating tests in ordinary implementation:**

- Heartbeat expiry and MQTT unavailability trigger the fake watchdog's exact fallback sequence.
- Stale retained `active` state cannot look healthy.
- Startup with Remote EMS on falls back.
- Fallback uses configured 8.8/9.6 kW and 100/0% values, never HA fallback number states.
- No watchdog path can enable Remote EMS.

**Separately authorized physical validation:** Validate HA configuration, then measure fallback for process stop/hang, MQTT loss, integration reload, HA restart, network/Modbus loss, and HA-host loss at conservative limits. These experiments are rollout work, not coding-harness work.

**Commits:** Keep PvOpti heartbeat/tests and external HA/watchdog configuration in their owning repositories and separate commits.

### Task 12: Wire safe deployment variables through Ansible

**Objective:** Expose all settings consistently while keeping role defaults and the live inventory non-actuating.

**Files:**
- External ansible-nas repository: `roles/energy_optimizer/defaults/main.yml`
- External ansible-nas repository: `roles/energy_optimizer/tasks/main.yml`
- Modify role Molecule/validation tests if present
- External site-config repository: `HpeNas/group_vars/nas/main.yml`

If those repositories are not available, add/verify the complete `EO_*` application contract in PvOpti and produce a deployment-variable manifest for the infrastructure owner. Do not create substitute Ansible files inside PvOpti.

**Rules:**

- Role defaults: `mode: dry_run`, battery control disabled, export disabled, arm token empty.
- Map every `energy_optimizer_*` variable to one `EO_*` environment variable; quote numeric/bool values consistently.
- Add Ansible assertions equivalent to application validation, including a multi-condition activation gate. A live deploy must fail if `mode=control` but watchdog, exact entity mapping, fallback mode, or both arming gates are absent.
- Keep `no_log` around tasks containing HA/Pstryk credentials and arm tokens.
- Pin a tested image digest/tag for each rollout stage; do not use an unreviewed floating tag for active control.
- Do not turn on battery export in the site inventory until the export-specific rollout stage passes.

**Verification:** In the owning infrastructure checkout, run its canonical role tests/lint, `ansible-playbook --syntax-check`, and an explicitly authorized check-mode deployment workflow. Inspect the rendered container environment with secrets redacted. A generic coding harness must not trigger a live deployment.

**Commits:**

- Ansible role: `feat: configure gated battery control`.
- Site config: `chore: prepare PvOpti battery control in dry run`.

### Task 13: Add deterministic simulation, replay, and control-aware backtesting

**Objective:** Prove economics and goal fulfillment before sending real commands.

**Files:**
- Modify: `src/energy_optimizer/simulator.py`
- Modify: `tests/test_simulator.py`
- Modify: `src/energy_optimizer/backtest.py` or the current backtest implementation location
- Modify: `src/energy_optimizer/web/routes.py` if new report fields are exposed

**Changes:**

- Simulate control cadence, plan refresh, inverter power/ramp/SoC limits, command delay, deadband, and fallback—not only ideal optimizer flows.
- Replay recorded telemetry/prices/forecasts without substituting Sigen data for missing Pstryk settlement intervals.
- Report gross energy value, battery degradation cost, import/export fees, command-induced throughput, fallback count, EV target misses, reserve violations, and delta versus current local EMS/self-consumption baseline.
- Add stress cases: price gaps, stale forecast, DST days, negative prices, PV error, load spikes, EV plug/unplug mid-plan, one-shot request, HA outage, and solver failure.

**Acceptance:** No simulated safety/goal violation in the release corpus. Economic claims are based only on settled intervals and include degradation/fees; incomplete settlement windows report incomplete data rather than a fabricated saving.

**Commit:** `test: replay live-control decisions before actuation`.

### Task 14: End-to-end fault-injection tests

**Objective:** Verify the complete control loop’s failure semantics, not only individual units.

**Files:**
- Create: `tests/test_battery_control_integration.py`
- Add reusable HA emulator fixtures under `tests/` if needed.

**Test matrix:**

- Happy-path grid charge, PV-first charge, self-consumption discharge, and export.
- Direction reversal with neutral transition.
- Stale telemetry/current price/plan.
- Padded current price, NaN entity, unknown mode option.
- HA 401/404/409/500, timeout, disconnect during a partial transaction.
- Correct entity read-back but wrong physical power direction.
- Manual mode/limit change during active control.
- App restart with remote EMS on.
- Duplicate app instance and lease expiry.
- DB unavailable after intent persistence.
- Heartbeat expiry and watchdog fallback.
- EV minimum target conflicts with export profitability.
- EV unplug, reaches target, or one-shot completes while a battery command is active.

**Commands:**

```bash
docker compose -f compose.dev.yml run --rm --no-deps app pytest tests/test_battery_control.py tests/test_sigenergy_control.py tests/test_battery_control_integration.py -v
make test
make lint
make typecheck
make fe-build
```

Expected: all tests pass; no test requires live HA or live inverter access.

**Commit:** `test: cover battery control faults and recovery`.

### Task 15: Documentation and operator runbook

**Objective:** Make arming, disarming, incident response, and rollback unambiguous.

**Files:**
- Modify: `DESIGN.md`
- Modify: `README.md`
- Create: `docs/control-runbook.md`

**Runbook content:**

- Architecture and authority boundaries.
- Exact preflight checklist and entity mapping.
- How to arm, disarm, force fallback, clear lockout, and verify physical state.
- How to test the HA watchdog.
- Expected control cadence and command expiry.
- Logs/API/MQTT fields used during diagnosis.
- Recovery from wrong power direction, stuck remote EMS, stale data, duplicate controller, and DB corruption.
- One-command/container rollback to the last dry-run image and explicit post-rollback verification that remote EMS is off.
- Known residual risks, especially HA host/inverter communication failure.

**Commit:** `docs: add PvOpti live-control runbook`.

## 5. Staged rollout and release gates

### Stage A — Characterization only

- **Status:** implementation baseline complete; preserved in `docs/sigenergy-control-contract.md` and commit `933e26e`.
- Dormant Standby, neutral behavior, 0.5 kW grid charging, ordinary fallback, and final safe state are documented.
- Failure-mode, discharge, export, and autonomous-timeout characterization remain explicit later-stage blockers. Do not repeat them during ordinary coding.

### Stage B — Shadow intent

- Deploy new code with `mode=dry_run`, battery control disabled.
- Generate/persist intents and control authorization, but execute no HA write.
- Compare intended power against what Sigen local EMS actually did for at least one representative solar/weather/price cycle.
- Gate: no stale-plan intents, no contradictory directions, EV targets/reserve always honored, and no unexpected lockout churn.

### Stage C — Watchdog rehearsal

- Deploy and test the HA-side watchdog/emergency-off path while PvOpti still cannot arm.
- Gate: bounded fallback proven for process stop, MQTT loss, HA restart, and integration reload, plus a separately proven HA-host-loss outcome through inverter timeout or a host-independent actor. Otherwise live control remains blocked.

### Stage D — Supervised low-power charge only

- **Status:** the physical 0.5 kW charge behavior was manually characterized, but application-controlled Stage D rollout has not begun.
- Set `mode=control` and battery-control enabled/armed only during attended windows.
- Keep battery export disabled. Clamp forced charge to a low value and a narrow SoC band.
- Gate: reliable limit acknowledgement exists, every command has matching mode/control acknowledgement and physical response, layered fallback and manual override tests pass, and no unplanned grid export occurs. The current HA number read-back defect blocks this gate.

### Stage E — Full-rate grid charging

- Raise charge limit gradually after reviewing audit data and economics.
- Gate: observed energy/cost tracks control-aware simulation within agreed tolerance and EV targets remain reliable.

### Stage F — Supervised low-power export

- Enable the separate export gate during attended high-price windows with conservative reserve and low export limit.
- Verify site/export permissions and no grid-limit violation.
- Gate: no goal/reserve violations, correct physical direction, and positive net value after degradation/fees.

### Stage G — Unattended control

- Remove attended-window restriction only after a defined observation period with zero unresolved safety events.
- Keep automatic lockout, external watchdog, two-key configuration, and emergency-off permanently.

At every stage, rollback is: disarm/fallback, verify remote EMS off and local control restored, deploy the previous dry-run image/config, then verify telemetry and EV scheduling. Never rely only on container rollback to clear an inverter command.

## 6. Acceptance criteria

Functional:

- PvOpti can command verified battery charge/discharge behavior from the current plan interval and can deliberately cause grid import/export only when separately permitted.
- EV minimum/departure and one-shot goals constrain stationary-battery decisions and are visible in explanations.
- Dry-run behavior remains the default and remains compatible with existing dashboards/API consumers.

Safety:

- Every active command has a fresh plan, fresh current price, fresh telemetry, a valid lease, healthy watchdog, and two activation gates.
- Stale/missing/unknown data, solver failure, HA failure, physical mismatch, manual override, lease loss, process stop, and restart lead to verified fallback or persistent lockout.
- The layered watchdog/fail-safe design is proven under the failure modes listed above, including an explicit HA-host-loss outcome.
- Battery/grid power, SoC, ramp, dwell, and site import/export limits cannot be exceeded by either the optimizer or adapter.

Economic/data integrity:

- Arbitrage happens only above loss, fee, degradation, and activation margins.
- Settled Pstryk intervals remain the sole billed import/export source; incomplete settlement is reported as incomplete.
- Backtests/replays include command realism and EV goals, not idealized battery-only profit.

Quality:

- Full pytest, ruff, mypy, frontend build/typecheck, Ansible syntax/lint/check-mode, HA config validation, and fault-injection suites pass.
- A safety-critical review finds no blocker before each live rollout stage.
- The repository contains no credentials, active arm token, or secret-bearing logs.

## 7. Risks and design trade-offs

- **HA is a convenient but indirect actuator.** It reduces new protocol code but introduces HA/integration availability and persistent-command risk. The watchdog and empirical fail-state gate are mandatory; direct Modbus is a future option only if HA cannot provide bounded safety.
- **ESS limits are global maxima, but HA acknowledgement is defective.** A 0.5 kW limit produced approximately 0.5 kW charging, while the HA number state remained fallback `0.0`. Closed-loop verification needs a reliable register acknowledgement plus physical ranges; never restore from displayed HA number state.
- **Grid flow is residual.** PV/load can change after planning, so a battery command may not produce the planned import/export. Receding-horizon control, conservative bounds, and physical feedback limit this risk; the controller must not chase second-to-second noise.
- **More conservatism reduces theoretical profit.** Deadbands, dwell, reserve, uncertainty margins, and fallback costs are intentional. Optimize realized net value, not solver objective alone.
- **SQLite lease scope.** It protects instances sharing the same DB volume, not independent deployments with separate DBs. Deployment assertions must enforce one replica; a HA-side ownership helper or external lock is needed if topology changes.
- **Manual override detection can cause nuisance lockouts.** This is preferable to the controller fighting a person. The UI/runbook should make re-arm easy and explicit.

## 8. Resolved facts and remaining live-rollout questions

Resolved by characterization:

- Exact installed entity IDs/options/ranges are in the empirical contract.
- Safe first activation uses dormant Standby while Remote EMS is off, then explicit limits, Remote EMS on into Standby, physical neutral, and only then an authorized command mode.
- Standby neutral is ±0.12 kW with at least a 15-second verification deadline.
- Charge/discharge registers are global maximum limits; a 0.5 kW charging limit was physically verified.
- Normal fallback restores explicit 8.8/9.6 kW and 100/0% values, turns Remote EMS off, and verifies local Maximum Self Consumption.

Remaining before application-controlled Stage D:

1. What reliable path will acknowledge the actual limit/cut-off register values despite the HA entity fallback-state defect?
2. Does the inverter enforce a command timeout, and what bounded fallback covers HA-host power loss and Modbus/network loss?
3. What exact site/DSO restrictions apply before any deliberate battery export?
4. Will every app restart require explicit operator re-arm? Initial rollout must require it.
5. What economic activation margin and clean shadow-observation period are required? Start conservative and tune from settled Pstryk results.
6. If FastAPI has no authenticated mutation boundary, what secure local mechanism owns `arm` and `clear-lockout`?
7. What low-power discharge mode/limit behavior is safe below current household load without export?

## 9. Final implementation verification checklist

- [x] Task 1’s control contract is complete enough for non-actuating implementation and reviewed.
- [ ] All production defaults are non-actuating.
- [ ] Both application and Ansible reject incomplete/unsafe live configuration.
- [ ] Intent translation is pure and fully unit tested.
- [ ] Optimizer cannot emit contradictory or forbidden flows.
- [ ] Control authorization is stricter than planning safety.
- [ ] HA service calls are idempotent, ordered, and read-back verified.
- [ ] Reliable number-register acknowledgement exists; fallback HA `0.0` is never treated as success/original state.
- [ ] Physical power direction/range is verified.
- [ ] Fallback is independent of planning and proven idempotent.
- [ ] Startup, shutdown, lease loss, and manual override are safe.
- [ ] HA-side watchdog and host-independent/HA-host-loss fallback outcome are fault-injection tested.
- [ ] EV goals suppress conflicting export and remain feasible.
- [ ] API/MQTT/UI distinguish intent, request, read-back, and physical result.
- [ ] Settled Pstryk data remains the only billed-energy truth.
- [ ] Full backend/frontend/Ansible/HA validation passes.
- [ ] Safety-critical review approves the exact image/config before each live stage.
- [ ] Rollback restores local EMS and is rehearsed before export is enabled.
