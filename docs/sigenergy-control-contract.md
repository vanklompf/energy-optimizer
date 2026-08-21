# Verified Sigenergy control contract

Status: **charge, discharge, and export characterized; HpeNas runs live control. Modbus-loss containment is open and accepted.**
Characterization date: 2026-08-08 (charge A/B/A). Discharge, export, and fallback evidence is in [`commissioning/`](./commissioning/), through 2026-08-20.
Site: Sigen Plant through Home Assistant

This document records only behavior observed on the installed system. Upstream names or assumed Modbus semantics are not treated as verified capabilities.

How to run live is [`OPERATIONS.md`](./OPERATIONS.md). The archived
[`battery-control-context.md`](./archive/battery-control-context.md) and the
[actual-control implementation plan](./archive/plans/2026-08-08_001527-pvopti-actual-energy-control.md)
are historical. This empirical document is authoritative when a plan, code
comment, or upstream integration assumption conflicts with it.

## Safety state after the 2026-08-08 characterization (snapshot, not policy)

The bullets below record cleanup after the first attended charge probe. They are
**not** the live operating posture. Live control keeps Remote EMS available,
`read_only: false`, and the six control entities enabled; see
[`OPERATIONS.md`](./OPERATIONS.md).

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

`Standby`, `Command Charging (Grid First)`, `Command Discharging (PV First)`, and
`Command Discharging (ESS First)` are physically tested. `Unknown` must never be
selected by PvOpti. `PCS Remote Control` and `V2G` are outside the intended
release. `Command Charging (PV First)` and `Maximum Self Consumption` under
Remote EMS remain untested as command modes (local MSC is the restored idle
state).

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

## Telemetry emission behavior

Observed 2026-08-17 from stored PvOpti telemetry, not from a dedicated test.

`sensor.sigen_plant_battery_state_of_charge` reports at 0.1% resolution and its
`last_updated` advances only when that value changes. Two consequences follow, both of
which produced real defects before they were understood:

- A pinned SoC (a full battery, or an idle one) stops emitting entirely. Across stored
  samples, 86% of readings at exactly 100% SoC exceeded the 10-minute SoC staleness
  threshold, against 7% below 100%. Entity age alone is therefore not evidence that the
  SoC feed is dead; liveness must be judged from another entity in the same integration.
- At 1 kW the pack moves 0.1% roughly every 65 seconds, so SoC cannot be expected to tick
  inside a 15-second command-verification window. Command verification must bound SoC by
  age and rely on battery power and grid flows to prove that a command took effect.

The fast power sensors (battery, PV, consumed, grid import, grid export) do update
within the 5-minute window while their values are changing, and likewise pin when their
value settles — zero being the common case overnight.

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

Stage 1 closed most of the original gaps. Characterized since this document was first written, each with a dated note in [`commissioning/`](./commissioning/):

- grid-import charge stepped to 4 kW, with limit tracking and import coverage;
- forced discharge to household load below and at net load, PV First, zero export;
- deliberate battery export at night on ESS First, stepped to 6 kW, inside the site export limit and reconciled against Pstryk settled sell data;
- `Command Discharging (PV First)` versus `(ESS First)` with PV present — ESS First curtails PV to zero at any discharge limit, so daylight export must use PV First;
- the discharge cut-off register taking the configured operating reserve;
- heartbeat expiry, Sigen integration reload, container stop, container pause, and HA restart, each from an active command, on both charge and discharge.

The following remain explicitly unverified:

- **Modbus/network path loss (failed, do not re-run).** HA served cached Sigen entities, the watchdog never fired, and the inverter held the last Remote EMS command for the full 90 s outage with no native timeout. This is the open blocker for unattended Stage 3 operation and is carried as accepted residual risk through Stage 2; see [`OPERATIONS.md`](./OPERATIONS.md);
- import charging above ~6.9 kW: the battery plateaued at 6.36 kW against a 6.86 kW command and never reached the 8.8 kW rating;
- persistence across inverter/gateway reboot;
- cut-off SoC enforcement boundaries and BMS-buffer interaction — PvOpti writes the reserve to the cut-off register but never relies on the inverter to enforce it, rejecting any sample within the configured margin of the floor;
- every mode except `Standby`, `Command Charging (Grid First)`, and the two discharging modes above; V2G and PCS Remote Control stay out of scope.

Number-register acknowledgement is promoted only for the version-bound evidence recorded above. Revalidate it on any HA, overlay, or command-semantic drift.
