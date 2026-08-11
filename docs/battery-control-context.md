# Battery-control implementation context

Status: repository handoff context for implementation; not an authorization to actuate the live inverter.

This document makes the battery-control plan self-contained for a coding harness that has only the PvOpti repository. It records site facts, user policy, external repository state, and empirical constraints that cannot be inferred safely from the application source.

## Source priority

When sources disagree, use this order:

1. `docs/sigenergy-control-contract.md` for empirical control behavior on the installed system.
2. Safety invariants and rollout gates in `docs/plans/2026-08-08_001527-pvopti-actual-energy-control.md`.
3. This document for site policy and external-system context.
4. Current code/tests and `DESIGN.md` for implemented application behavior.
5. Upstream Sigenergy integration or Modbus documentation for background only; never use upstream assumptions to override an empirical result.

## Handoff and repository boundaries

PvOpti is one of three repositories involved in eventual rollout:

| Repository | Purpose | Characterization commit |
|---|---|---|
| PvOpti | Application, optimizer, control plane, tests, API/UI | `933e26e` (`docs: record verified Sigenergy control contract`) |
| ansible-nas | Generic Home Assistant and PvOpti deployment roles | `44913bd3` (`feat: deploy Home Assistant component overlays`) |
| AnsibleNasConfigs | Site inventory, HA configuration, Sigen integration overlay | `728e701` (`fix: expose dormant Sigenergy EMS mode`) |

The other repositories may not be available to an external coding harness. Application tasks must not assume they are mounted. Cross-repository work is a separate workstream and must be committed in its owning repository.

At handoff, PvOpti contained unrelated optimizer/EV hard-floor work that predated the documentation commits. That work was inspected during Task 0 and preserved as `36ce7e0` (hard-floor settings), `b676d51` (MILP EV/reserve flows), and `0af865d` (service/API wiring). Untracked `opencode.json` remains local editor tooling and is not part of the battery-control plan.

## Installed site and sign conventions

| Item | Installed/verified value |
|---|---|
| Home Assistant after characterization deployment | 2026.8.1 |
| HA custom integration | Sigenergy Local Modbus `v.1.2.5.1` |
| Inverter | Sigen Hybrid 6.0 TP2 |
| Inverter firmware | `V100R001C10SPC113` |
| Battery usable/rated telemetry | approximately 18.08 kWh |
| ESS rated charging power | 8.8 kW |
| ESS rated discharging power | 9.6 kW |
| Inverter AC limit used by the current model | 6.0 kW |
| Site import limit currently modeled | 11 kW |
| Site export limit currently modeled | 6.6 kW; this is not permission for deliberate battery export |
| Time zone | `Europe/Warsaw` |

Sigen telemetry convention used by this project:

- battery power `> 0`: charging;
- battery power `< 0`: discharging;
- grid import and export are separate non-negative telemetry values;
- PvOpti commands battery behavior, not billed grid flow. Grid flow is the residual of PV, household/EV load, and battery power.

The 6.0 kW inverter AC constraint, 8.8/9.6 kW ESS-side limits, and 11/6.6 kW modeled site limits are different constraints and must not be substituted for one another. In particular, a configured export limit is not proof of DSO permission for deliberate battery export.

SoC freshness is boundary-aware. A battery pinned at 100% (or a configured lower boundary) may legitimately report an unchanged value while polling and faster power/grid telemetry remain healthy. Do not classify boundary-pinned SoC as stale solely because its numeric value did not change; require a fresh state update/poll timestamp and corroborating telemetry. This exception does not make an old or unavailable SoC state fresh.

## Exact Home Assistant control mapping

The empirical contract contains full options/ranges. Core IDs are:

```text
switch.sigen_plant_remote_ems_controlled_by_home_assistant
select.sigen_plant_remote_ems_control_mode
number.sigen_plant_ess_max_charging_limit
number.sigen_plant_ess_max_discharging_limit
number.sigen_plant_ess_charge_cut_off_state_of_charge
number.sigen_plant_ess_discharge_cut_off_state_of_charge
```

Important modes:

```text
Standby
Command Charging (Grid First)
Command Charging (PV First)
Command Discharging (PV First)
Command Discharging (ESS First)
Maximum Self Consumption
PCS Remote Control
V2G
```

Only `Standby` and `Command Charging (Grid First)` have been physically characterized. Discharge, export, PCS Remote Control, V2G, and the remaining command modes are not authorized.

## Deployed integration overlay

The stock integration hides the mode selector while Remote EMS is off, creating an unsafe first-activation ordering problem. The site deployment carries a one-line overlay based on exact tag `v.1.2.5.1`:

```python
available_fn=lambda data, _: True  # dormant mode remains readable/writable while Remote EMS is off
```

This allows dormant `Standby` to be written and verified before enabling Remote EMS. Application startup validation must verify the required capability at runtime; it must not assume the deployment overlay exists merely because these documents mention it. A HACS update can replace live custom-component files until the Ansible overlay is reapplied.

## Current safe live state after characterization

At the end of the supervised test window:

- Sigen integration `read_only: true`;
- all six writable entities disabled in HA and absent from live state;
- Remote EMS off (`plant_remote_ems_enable = 0`);
- dormant mode Standby (`plant_remote_ems_control_mode = 1`);
- explicit local limits restored: 8.8 kW charge, 9.6 kW discharge, 100% charge cut-off, 0% discharge cut-off;
- local `Maximum Self Consumption` resumed.

A coding harness must not alter this state.

## Empirical transaction contract

Safe activation order established by supervised testing:

1. Verify Remote EMS is off.
2. Write and verify dormant `Standby` while Remote EMS remains off.
3. Write conservative global limits from explicit site configuration.
4. Enable Remote EMS into Standby.
5. Wait for fresh physical telemetry to enter the neutral band.
6. Select an authorized command mode.
7. Verify physical direction, magnitude, grid flow, and SoC within a bounded deadline.

Safe normal fallback:

1. Select Standby.
2. Wait for physical neutral.
3. Restore explicit local limits/cut-offs from configuration—not HA number states.
4. Turn Remote EMS off.
5. Verify raw Remote EMS off and local `Maximum Self Consumption` behavior.

Measured values:

- Standby neutral is not exact zero; use an empirical band of ±0.12 kW.
- Allow at least 15 seconds for physical Standby verification.
- A 0.5 kW grid-charge command settled at 0.497–0.498 kW.
- Physical charge response crossed 0.4 kW after about 5.4 seconds.
- Charge-to-Standby removed intentional charging after about 5.7 seconds, then held a small residual discharge.

## Critical HA number read-back defect

Sigenergy Modbus V2.9 defines registers 40032/40034 as global ESS maximum charge/discharge limits. They apply even when Remote EMS is off.

On this installation, the HA number entities display fallback `0.0` rather than reliable register values. A successful HA response and unchanged `0.0` state do not acknowledge a write. During characterization, writing cleanup value `0.0` physically stopped ordinary local battery operation; restoring 8.8/9.6 kW resumed local self-consumption.

Consequences for implementation:

- never use HA's displayed limit state as the original value;
- never restore `0.0` from entity state;
- keep explicit configured local defaults;
- mark limit acknowledgement unsupported/unverified until a reliable register-read path is implemented;
- physical response is required but does not, by itself, satisfy the plan's independent command-read-back invariant;
- live arming remains blocked until the read-back gap is resolved or the safety design is explicitly revised and reviewed.

## Household battery and EV policy

Current operating reserve is **15%**.

- Ordinary household discharge, economic battery export, and ordinary opportunistic EV charging preserve the 15% operating reserve.
- Guaranteed Mercedes departure charging may consume that operating reserve when necessary to meet the deadline; it remains bounded by the inverter/BMS hard floor.
- A guaranteed departure minimum is a deadline-constrained energy requirement, not an immediate continuous-charge command.
- Explicit one-shot Mercedes charging may use grid energy.
- Normal opportunistic Mercedes charging must not deliberately import grid energy.
- Normal opportunistic charging prioritizes the Mercedes over filling the stationary battery.
- It may bridge a current PV deficit from stationary battery above reserve only when conservative same-day forecast surplus supports later recovery/filling of the stationary battery.
- EV relay control and stationary-battery control are separate actuators with separate gates and failure handling.

Do not hard-code a stale policy value from historical notes. Use current application settings and preserve these policy relationships in tests.

## Billing and economic truth

Pstryk's settled hourly meter intervals are the sole source of billed grid import/export for accounting, savings, and backtests.

- Do not substitute Sigen grid telemetry when a Pstryk settlement interval is missing.
- Missing, null, or still-open settlement intervals remain incomplete.
- Sigen telemetry is authoritative only for live behind-the-meter PV, battery, SoC, load, and control feedback.
- Economic activation must include fees, losses, degradation, uncertainty, and a configured margin.
- Padded current prices may support a recommendation but may not authorize live economic control.

## Development and deployment boundary

Implementation and ordinary CI are non-actuating. Use fake HA clients or a local emulator, dummy credentials, and deny outbound network access where the test runner permits it. Tests must fail if configured with a live-looking HA URL/token. Do not contact the live HA instance or use live credentials.

Current deployment facts that affect the control design:

- the site sets `energy_optimizer_available_externally: true`; Authentik OIDC
  gates the UI/API when `energy_optimizer_oidc_enabled` is true, but HTTP `arm`
  and `clear-lockout` mutation endpoints are still withheld until that control
  surface is intentionally exposed behind the authenticated boundary;

- the role deploys one Docker container with one `/data` volume, and PvOpti uses one SQLite database there;
- a SQLite lease protects processes sharing that database, not a second deployment with a different volume, so deployment validation must enforce one replica/site owner.

The deployment repositories use these owning paths when cross-repository tasks are eventually authorized:

```text
ansible-nas/roles/energy_optimizer/
ansible-nas/roles/homeassistant/
AnsibleNasConfigs/HpeNas/group_vars/nas/
AnsibleNasConfigs/HpeNas/homeassistant/
```

PvOpti documents the shared heartbeat/fallback contract in
`docs/battery-control-watchdog-interface.md`. Site HA automations belong in
`AnsibleNasConfigs/HpeNas/homeassistant/` and must stay disabled until physical
watchdog validation passes.

Live deployment is a separate operator-authorized activity. Application code completion does not authorize changing HA `read_only`, enabling entities, setting `mode=control`, installing an arm token, or actuating the battery.

## Outstanding release blockers

Before unattended or economic live control:

- implement reliable control-register acknowledgement/read-back;
- implement and prove an independent watchdog/fallback actor;
- test process stop/hang, MQTT loss, HA restart, integration reload, network/Modbus loss, and HA-host loss;
- characterize low-power discharge below household load without export;
- confirm export permission/site constraints before deliberate export testing;
- verify restart reconciliation, single-owner lease, manual intervention, fallback failure, and persistent lockout;
- complete shadow observation and staged rollout gates in the plan.

The absence of those proofs does not block implementation in dry-run/shadow mode. It blocks live arming.
