# PvOpti attended read-back and watchdog commissioning plan

> **For Hermes:** Execute only one numbered stage at a time, with the operator present for every stage that changes HA, enables write access, or can alter Sigen behaviour.

**Goal:** Convert the completed offline safeguards into evidence for a safe initial charge-only commissioning decision, while keeping PvOpti disarmed unless every hard gate is independently proven.

**Architecture:** PvOpti stays in `dry_run`; Home Assistant remains the only control surface. The local Sigen Modbus overlay is a read-path repair only. HA-side watchdog cleanup is independent of the PvOpti process and must never enable Remote EMS. Direct Modbus remains diagnostic-only (`0x03` reads only).

**Hard constraints:** no discharge/export, no Remote EMS enablement or writable-entity activation outside an attended stage, no trust in HTTP success or a fallback `0.0`, and no use of displayed HA number values as restore sources.

---

## Current evidence

- PvOpti offline suite is green: `271 passed`.
- Offline gates now reject fallback-zero, unavailable, stale-timestamp, cancellation-interrupted fallback, startup HA-read failure, and HA-unreachable fallback.
- Prepared HA overlay: `HpeNas/homeassistant/custom_components/sigen/modbus.py` based on upstream commit `f887c8cf533ba0213a186494d86ffbb05f8c6d45`.
- Overlay tests prove serial probing, type-safe read caching, and `None` rather than fallback `0` for unavailable registers.
- Direct read-only reference remains: 40032 = 8.8 kW and 40034 = 9.6 kW at Modbus unit 247.
- Outstanding gates: live HA read-back after the overlay, independent HA watchdog commissioning, failure-path evidence, and operator authorization for the A/B/A probe.

## Stage 0 — Review and preserve safe defaults

**Objective:** Ensure the deployment inputs cannot accidentally arm control.

**Files / surfaces:**
- `/mnt/code/PvOpti/src/energy_optimizer/{ha_client.py,sigenergy_control.py,service.py}`
- `/mnt/code/AnsibleNasConfigs/HpeNas/homeassistant/custom_components/sigen/modbus.py`
- `/mnt/code/AnsibleNasConfigs/HpeNas/homeassistant/automations/pvopti_battery_watchdog.yaml`
- `HpeNas/group_vars/nas/main.yml` (secrets redacted in review)

1. Review diffs and keep `EO_MODE=dry_run`, battery control disabled, export disabled, arm token empty, and `NUMBER_REGISTER_ACK_RELIABLE=false`.
2. Do not mix unrelated working-tree changes into the deployment review.
3. Re-run:
   ```bash
   cd /mnt/code/PvOpti
   .venv/bin/python -m pytest -q
   ```
4. Verify no telemetry/control source setting has enabled a mutable HA entity or Remote EMS.

**Pass condition:** all safe defaults remain in force; any uncertainty stops the rollout.

## Stage 1 — Attended, read-only HA overlay deployment

**Objective:** Deploy only the Sigen `modbus.py` read-path overlay and prove it observes the global ESS limits correctly across refresh/restart.

1. Record the baseline, using only read paths:
   - HA entity availability/state/`last_updated` for both ESS limits;
   - raw read-only Modbus values 40032 / 40034;
   - Remote EMS is off; mode is Standby/local; Sigen writes remain disabled.
2. Have the operator approve the HA restart implied by the Ansible overlay-copy task.
3. Deploy the overlay only; do not change integration `read_only`, entity registry, or PvOpti actuation variables.
4. After HA comes up, wait for at least two coordinator refreshes and record fresh HA states.
5. Reload/restart HA once more, then repeat the two-refresh observation.

**Required evidence:**
- HA entities expose 8.8 and 9.6 kW (not fallback 0, `unknown`, or stale cache);
- timestamps advance on fresh coordinator reads;
- independent raw read-only register values match;
- Remote EMS remains off and normal local operation is unchanged.

**Fail condition:** mismatched, unavailable, malformed, or stale values. Revert the overlay / remain dry-run; do not proceed to writable entities.

## Stage 2 — Watchdog configuration and dry-path rehearsal

**Objective:** Configure the HA-side watchdog while it remains disabled, then prove its trigger and action model without enabling Remote EMS.

1. Validate YAML and exact entity IDs against HA’s live registry.
2. Confirm watchdog actions are only: Standby, explicit restores `8.8/9.6/100/0`, Remote EMS off, notification.
3. Confirm there is no `switch.turn_on` action or indirect route that enables Remote EMS.
4. Configure heartbeat topic and `input_datetime` / emergency helper references without enabling the automation.
5. Use HA traces/templates or a non-actuating dry rehearsal to check:
   - no heartbeat timestamp;
   - expired heartbeat with no new MQTT traffic;
   - retained/future/malformed heartbeat rejection;
   - emergency helper on;
   - HA startup with Remote EMS reported on.

**Pass condition:** each unsafe condition selects the fallback branch; a fresh non-retained heartbeat selects no-write healthy behaviour.

## Stage 3 — Attended A/B/A read-back and physical-response probe

**Objective:** Prove the live acknowledgement contract with a small, reversible charge-limit change.

**Preconditions:** Stage 1 pass; a stable daytime or controlled low-load window; operator present; writable entities enabled only for the window; Remote EMS still off; direct raw reads working; explicit restoration values recorded.

1. Record A: HA and raw values at 8.8/9.6 plus physical baseline.
2. Write a low, safe charge limit B (for example 0.5 kW) under the approved supervised procedure.
3. Require all of the following before considering B acknowledged:
   - HA shows the matching B value with a timestamp newer than the pre-write state;
   - raw read-only register 40032 matches B;
   - physical battery/grid response matches the characterized safe response within the bounded deadline.
4. Restore A (8.8 kW), then independently confirm HA + raw register + physical recovery.
5. Disable writable entities and restore integration `read_only: true` after the probe.

**Fail condition:** any missing or conflicting evidence triggers the supervised fallback sequence and preserves `NUMBER_REGISTER_ACK_RELIABLE=false`.

## Stage 4 — Attended fallback and containment rehearsal

**Objective:** Prove the cleanup sequence independently from ordinary command success.

1. From a deliberately supervised low-power state, validate the sequence:
   `Standby → physical neutral (±0.12 kW) → 8.8/9.6/100/0 → Remote EMS off → local behaviour`.
2. Trigger one watchdog path after a real heartbeat has been observed: stop PvOpti or block its MQTT heartbeat; wait beyond the 60-second expiry.
3. Confirm HA watchdog performs the fallback, emits a notification, and never turns Remote EMS on.
4. Separately test restart/reload only in Standby first:
   - PvOpti/container restart;
   - HA integration reload;
   - HA restart.
5. After every scenario, verify Remote EMS off, explicit restores, local Maximum Self Consumption / normal physical behaviour, and an auditable lockout/action record.

**Release blocker:** do not test unattended operation until HA-host-loss / network-loss containment has a separately approved, independent recovery path.

## Stage 5 — Controlled rollout decision

Only after Stages 1–4 pass with recorded evidence:

1. Set `EO_BATTERY_CONTROL_NUMBER_REGISTER_ACK_RELIABLE=true` only for the proven integration version/overlay checksum.
2. Enable the HA watchdog automation.
3. Keep export and discharge permanently disabled for this release.
4. Use a new explicit arm authorization and deploy in charge-only mode for a single attended interval.
5. Observe every command and its fallback; then return to dry-run unless a later review explicitly approves a longer supervised trial.

**Not in scope:** discharge, export, V2G, PCS Remote Control, cut-off boundary testing, unattended operation, or direct Modbus writes.
