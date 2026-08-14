# Verified Sigenergy control contract

Status: **supervised Standby and 0.5 kW grid-charge behavior verified; unattended control not approved**
Characterization date: 2026-08-08
Site: Sigen Plant through Home Assistant

This document records only behavior observed on the installed system. Upstream names or assumed Modbus semantics are not treated as verified capabilities.

Implementation consumers should also read [`battery-control-context.md`](./battery-control-context.md) and the [actual-control implementation plan](./plans/2026-08-08_001527-pvopti-actual-energy-control.md). This empirical document is authoritative when a plan, code comment, or upstream integration assumption conflicts with it.

## Safety state after characterization

- Remote EMS is verified `off` in HA and raw diagnostics (`plant_remote_ems_enable = 0`).
- The dormant Remote EMS mode is verified `Standby` (`plant_remote_ems_control_mode = 1`).
- The Sigen integration was returned to `read_only: true` after supervised testing.
- Every writable entity listed below is disabled in the HA entity registry (`disabled_by: user`) and absent from `/api/states`.
- Global ESS limits were restored to the installed ratings: 8.8 kW charge, 9.6 kW discharge, 100% charge cut-off, and 0% discharge cut-off.
- Normal local `Maximum Self Consumption` behavior resumed after cleanup.
- Final observed baseline:
  - battery SoC: 53.7%;
  - battery power: approximately -0.36 kW (negative means discharge);
  - household consumption: approximately 0.24 kW;
  - PV: 0.000 kW;
  - grid import: approximately 0.002 kW;
  - grid export: 0.000 kW;
  - reported plant EMS work mode: `Maximum Self Consumption`.

## Installed versions and equipment

| Item | Empirical value |
|---|---|
| Home Assistant | 2026.8.1 (pulled during the supervised patch deployment) |
| Integration domain | `sigen` |
| Integration title/state | `Sigen Plant` / loaded |
| Custom integration update entity | `update.sigenergy_ess_update` |
| Installed integration version | `v.1.2.5.1` |
| Latest integration version reported by HA | `v.1.2.5.1` |
| Inverter model | Sigen Hybrid 6.0 TP2 |
| Inverter firmware | `V100R001C10SPC113` |

## Verified Home Assistant entity mapping

### Remote EMS authority switch

| Property | Value |
|---|---|
| Entity ID | `switch.sigen_plant_remote_ems_controlled_by_home_assistant` |
| Friendly name | Sigen Plant Remote EMS (Controlled by Home Assistant) |
| State with local/default control active | `off` |
| Default registry state before characterization | disabled by integration |
| Registry state after cleanup | disabled by user |

The entity was enabled and the Sigen integration reloaded. It read back `off` immediately after reload. Enabling the entity in the registry and reloading the integration did not turn Remote EMS on or materially disturb normal physical behavior.

Turning this switch on from preselected `Standby` and turning it off from both Standby and command charging were tested. OFF restored local `Maximum Self Consumption` behavior.

### Remote EMS mode

| Property | Value |
|---|---|
| Entity ID | `select.sigen_plant_remote_ems_control_mode` |
| Stock integration state while Remote EMS is off | `unavailable` |
| Patched test state while Remote EMS is off | dormant mode value remains readable/writable |

Options advertised by the installed entity:

1. `PCS Remote Control`
2. `Standby`
3. `Maximum Self Consumption`
4. `Command Charging (Grid First)`
5. `Command Charging (PV First)`
6. `Command Discharging (PV First)`
7. `Command Discharging (ESS First)`
8. `V2G`
9. `Unknown`

`Standby` and `Command Charging (Grid First)` were physically tested. All other options remain untested. `Unknown` must never be selected by PvOpti. `PCS Remote Control` and `V2G` are outside the intended first release.

### ESS power limits

| Purpose | Entity ID | Observed state | Min | Max | Step | Unit |
|---|---|---:|---:|---:|---:|---|
| Maximum charge | `number.sigen_plant_ess_max_charging_limit` | misleading fallback `0.0` | 0 | 100 | 0.001 | kW |
| Maximum discharge | `number.sigen_plant_ess_max_discharging_limit` | misleading fallback `0.0` | 0 | 100 | 0.001 | kW |

Sigenergy Modbus Protocol V2.9 defines registers 40032/40034 as global maximum charge/discharge limits with ranges from zero to rated power. They apply globally regardless of EMS mode. A 0.5 kW charge limit produced 0.497–0.498 kW battery charging, verifying maximum-limit behavior on this installation.

The stock integration did not poll/read these holding registers reliably on this installation. The deployed overlay serializes holding-register probes and keeps input- and holding-register caches separate; the exact overlay identified below produced fresh HA readings that matched independent raw reads during the attended 0.5 kW A/B/A test. Reliability is therefore promoted only for that exact HA/overlay identity and command semantics—not for arbitrary Sigen versions or entities.

The HA entity advertises a 100 kW maximum even though the installed ESS ratings are 8.8 kW charge and 9.6 kW discharge. The HA maximum is not a safe site capability.

### ESS cut-off SoC

| Purpose | Entity ID | Observed state | Min | Max | Step | Unit |
|---|---|---:|---:|---:|---:|---|
| Charge cut-off | `number.sigen_plant_ess_charge_cut_off_state_of_charge` | 100.0 | 0 | 100 | 0.01 | % |
| Discharge cut-off | `number.sigen_plant_ess_discharge_cut_off_state_of_charge` | 0.0 | 0 | 100 | 0.01 | % |

Writes on the stock integration initially shared the same unreliable read-back issue as the power limits. Under the version-bound overlay, fresh cut-off readings also matched independent raw function-03 reads during attended commissioning. Enforcement at the configured cut-off boundary, persistence across inverter restart, and interaction with the Sigen/BMS safety buffer remain untested.

## Registry and reload behavior

- Changing a disabled entity to enabled through `config/entity_registry/update` did not instantiate it immediately.
- The installed integration required `homeassistant.reload_config_entry` for its config entry before the entity appeared.
- Normal telemetry and each enabled entity returned approximately one second after each integration reload during this test.
- The Remote EMS switch remained `off` after every reload.
- The public registry API cannot restore `disabled_by: integration`; cleanup used the supported safe state `disabled_by: user`.

## Timeout/watchdog discovery

A read-only search of all installed Sigen entity-registry entries found no entity whose name indicates:

- command duration;
- command expiry;
- autonomous timeout;
- heartbeat; or
- watchdog.

This proves only that no such capability is exposed through the installed HA entity surface. It does **not** prove that the inverter lacks an internal timeout. Until active failure characterization proves a bounded autonomous expiry or an independent watchdog restores local control, a persistent forced command after controller/HA failure remains a release blocker.

## Supervised active characterization

### Exact installed-source behavior and safety patch

Source tag `v.1.2.5.1` (`f887c8cf533ba0213a186494d86ffbb05f8c6d45`) was inspected. It confirms:

- holding register 40029 controls Remote EMS enable;
- enabling it changes plant EMS work mode to Remote EMS;
- holding register 40031 controls Remote EMS mode;
- the stock HA selector is unavailable unless `plant_remote_ems_enable == 1`;
- the integration exposes no domain-level dormant-parameter write service;
- upstream `v.1.2.6` retains the same selector-availability condition.

Raw diagnostics initially showed dormant mode `0` (`PCS Remote Control`). Enabling Remote EMS in that unknown command context was rejected as unsafe.

A reproducible one-line overlay was therefore deployed through Ansible: the Remote EMS mode selector remains available while Remote EMS is off. The overlay is the exact `v.1.2.5.1` `select.py` with only its `available_fn` changed. The Ansible Home Assistant role now copies inventory-owned `custom_components` overlays and restarts HA when an overlay changes.

The patch made dormant mode readable and writable without enabling Remote EMS. `Standby` was written and independently verified through raw diagnostics:

- `plant_remote_ems_enable = 0`;
- `plant_remote_ems_control_mode = 1` (`Standby`).

This dormant Standby value persisted after the active tests and remains the final raw mode value.

### Integration write gate

The first writes were intentionally rejected because the integration option was `read_only: true`; HA logged `Cannot write parameter while in read-only mode`. For the supervised window only:

1. the official HA integration-options flow changed `read_only` to `false` while preserving host, port, slave ID, and scan interval;
2. Remote EMS remained off during that change;
3. after testing, the same official flow restored `read_only: true`.

Production automation must retain a separate explicit arming gate; changing this integration option alone is not an acceptable production arming mechanism.

### Global ESS-limit semantics and HA read-back defect

Sigenergy Modbus Protocol V2.9 states that registers 40032/40034 are global maximum charging/discharging limits and take effect regardless of EMS mode. The stock installed integration's number entities did not provide reliable holding-register read-back: their state remained fallback `0.0` after successful writes. The deployed overlay later corrected this by serializing probes and separating holding/input cache keys. Fresh number states then matched independent raw reads during the attended 0.5 kW A/B/A evidence. Promotion remains version-bound to HA 2026.8.1, overlay commit `fd238d2`, `modbus.py` SHA-256 `f17b7bbc66d33513b0dec66223ad90d2e9e9d7dad3d1cbc489d8409a448f5101`, and the recorded command semantics.

This caused an important characterization incident: a cleanup write of `0.0` physically disabled ordinary local battery charge/discharge even though the entity state appeared unchanged. The effect was detected through physical telemetry (battery near zero and household load imported from grid). Writing the installed ratings—8.8 kW charge and 9.6 kW discharge—restored local self-consumption after approximately five seconds.

Required contract:

- treat number-state acknowledgement as reliable only for the recorded HA/overlay/evidence identity; any identity drift resets the gate to false;
- require a fresh post-write timestamp and exact value; reject cached, unavailable, malformed, or fallback-zero states;
- never "restore" these registers from an unverified displayed state;
- maintain explicit configured safe/local values outside HA state;
- keep bounded physical battery/grid verification and conservative site clamps mandatory even when register acknowledgement passes;
- on cleanup restore 8.8/9.6 kW and 100/0% cut-offs for this installation, then verify local behavior.

### Verified Standby behavior

Remote EMS was enabled only after dormant mode had been written and verified as Standby.

Observed behavior:

- plant EMS mode changed to `Remote EMS`;
- Standby removed intentional charge/discharge;
- a small residual battery flow remained around -0.087 to -0.095 kW after a charge-to-Standby transition;
- entering physical Standby from local operation reached the conservative neutral band after 9.86 seconds;
- entering physical Standby from 0.5 kW charging removed charging after approximately 5.72 seconds.

Operational neutral must therefore not mean exactly 0.000 kW. For this installation, use an empirical Standby band of **±0.12 kW**, a verification deadline of at least **15 seconds**, and fresh telemetry across the transition. HA selector state changes before physical power settles.

### Verified low-power grid charging

With global limits written to 0.5 kW charge and 0.0 kW discharge, charge cut-off set 0.5 percentage points above current SoC, and dormant mode verified Standby:

1. Remote EMS was enabled into Standby;
2. mode changed to `Command Charging (Grid First)`;
3. battery charging first appeared at 0.260 kW and settled at 0.497–0.498 kW;
4. grid import settled around 0.866–0.926 kW with the household load included;
5. grid export remained 0.000 kW;
6. the 0.4 kW physical-response threshold was reached after 5.40 seconds;
7. mode returned to Standby;
8. rated limits/cut-offs were restored;
9. Remote EMS was turned off;
10. local `Maximum Self Consumption` resumed, with battery discharge around 0.36 kW and negligible grid import.

This verifies that 40032 acts as a maximum charge limit and that low-power grid charging is physically controllable. It does not authorize economic grid charging in production.

### Verified fallback

The following OFF sequence was tested from active Remote EMS control:

1. select `Standby`;
2. wait for physical charge/discharge to enter the ±0.12 kW neutral band;
3. restore explicit local/global limits and cut-offs;
4. turn Remote EMS off;
5. verify raw Remote EMS enable is zero;
6. verify local `Maximum Self Consumption` and normal battery/grid behavior resume.

Turning Remote EMS off is also idempotent when already off. This proves the ordinary supervised fallback path. It does not prove fallback after HA, network, Modbus, or controller failure.

## Current operational contract

A future PvOpti adapter must follow this order:

1. Require explicit arming, fresh telemetry, operator-independent watchdog, and the integration write gate enabled.
2. Verify Remote EMS is off.
3. Write and verify dormant `Standby` while Remote EMS remains off.
4. Write conservative global limits from explicit configuration; do not use HA number states as originals.
5. Enable Remote EMS and require Standby.
6. Wait up to at least 15 seconds for physical battery power to enter ±0.12 kW.
7. Select the intended command mode.
8. Verify physical direction, magnitude, grid flow, and SoC within a bounded deadline; otherwise fall back and lock out.
9. For direction changes, pass through Standby and verify the physical neutral band before selecting the opposite direction.
10. On normal release, select Standby, verify neutral, restore explicit local limits, turn Remote EMS off, and verify local behavior.

## Untested behavior and rollout blockers

The following remain explicitly unverified:

- low-power forced discharge to household load without deliberate export;
- every mode except `Standby` and `Command Charging (Grid First)`;
- persistence across inverter/gateway reboot;
- behavior during HA restart while Remote EMS is active;
- Sigen integration reload while Remote EMS is active: passed attended on 2026-08-14 for the bounded 0.5 kW charge-only path after correcting the entity-readiness race;
- Modbus loss, network loss, application/container stop, and service timeout after physical actuation;
- autonomous command expiry;
- an independent watchdog capable of restoring local control when HA/PvOpti cannot;
- deliberate battery export and site/export-limit enforcement;
- cut-off SoC enforcement boundaries and BMS-buffer interaction;
- reliable register read-back for power limits and cut-offs.

No unattended live-control adapter may be armed while the failure-path and watchdog blockers remain unresolved.

## Next supervised test stage

The next separately authorized maintenance window should focus on failure containment, not export:

1. number-register acknowledgement is now promoted only for the version-bound 2026-08-14 charge evidence; revalidate and reset the gate on any HA/overlay/command-semantic drift;
2. establish an independent watchdog/fallback actor;
3. test low-power discharge below current household load, with zero-export monitoring and immediate Standby fallback;
4. test HA integration reload and HA restart while in Standby before testing them during a low-power command;
5. test PvOpti/container stop and Modbus interruption only after the watchdog is independently proven;
6. restore read-only mode, disable the writable entities, and verify local behavior after every scenario.

Deliberate export, V2G, PCS Remote Control, and unattended operation remain excluded.
