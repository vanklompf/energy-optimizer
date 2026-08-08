# PvOpti deployment variable contract (`EO_*`)

Status: Task 12 review artifact. Application settings are owned by PvOpti
(`.env.example` / `Settings`). Ansible role/site inventory map them to container env.
Do not invent substitute Ansible roles inside this repository.

## Task 12 review (2026-08-08)

| Requirement | Status |
|---|---|
| Role defaults: `dry_run`, control off, export off, empty arm token | Met in uncommitted `ansible-nas` role worktree |
| Map battery-control `energy_optimizer_*` → `EO_*` | Met |
| Activation asserts (gates, entities, watchdog, fallback) | Met |
| Reject floating image for `mode=control` | Met (`image_digest` or non-`latest`/`local` tag) |
| `no_log` around HA/Pstryk/arm | Met |
| Site inventory non-actuating | Met (`mode=dry_run`, control/export off) |
| Ansible commits | Deferred — keep changes uncommitted until an infra-owner commit |

Owning paths (external):

```text
ansible-nas/roles/energy_optimizer/defaults/main.yml
ansible-nas/roles/energy_optimizer/tasks/main.yml
ansible-nas/roles/energy_optimizer/docs/energy_optimizer.md
AnsibleNasConfigs/HpeNas/group_vars/nas/main.yml
```

## Non-actuating defaults (must remain)

| Variable | Safe default |
|---|---|
| `EO_MODE` / `energy_optimizer_mode` | `dry_run` |
| `EO_BATTERY_CONTROL_ENABLED` | `false` |
| `EO_BATTERY_CONTROL_ARM_TOKEN` | empty |
| `EO_BATTERY_EXPORT_ENABLED` | `false` |
| `EO_BATTERY_CONTROL_NUMBER_REGISTER_ACK_RELIABLE` | `false` |
| Watchdog entity IDs | empty until proven |

Arm token required for `mode=control`: `pvopti-battery-control-armed`.

Planner flags `EO_ALLOW_GRID_CHARGING` / `EO_ALLOW_BATTERY_EXPORT` are **not** physical
actuation gates and must not be treated as arming.

## Battery-control `EO_*` surface

See `.env.example` for the authoritative list. Categories:

1. Gates: mode, enable, arm token, export, grid-charge authorize, discharge/export authorize, ack reliable, supported directions.
2. Entities: Remote EMS switch, mode select, charge/discharge limits, charge/discharge cut-offs, watchdog health/ack.
3. Mode strings: Standby, Command Charging/Discharging variants, Maximum Self Consumption, command/fallback modes.
4. Local restore: 8.8 / 9.6 kW and 100 / 0% cut-offs (never HA displayed `0.0`).
5. Limits/timings: import/export/charge/discharge caps, SoC bounds, cadence, plan/telemetry age, settle/poll/timeout, physical verify ≥15s, heartbeat interval/expiry, dwell, deadband, ramp, retry, lockout, activation margin.

## Control-mode deploy gate (Ansible)

A live deploy with `energy_optimizer_mode=control` must fail unless:

- `battery_control_enabled` is true
- arm token matches `pvopti-battery-control-armed`
- `number_register_ack_reliable` is true
- all six EMS entity IDs are non-empty
- both watchdog entity IDs are non-empty
- fallback mode is set and Remote EMS-off restore is required
- image is immutable (`energy_optimizer_image_digest` set, or a non-floating reviewed tag — not `latest`/`local`)

## Site inventory policy

Keep HpeNas at dry-run until staged rollout gates pass. Do not enable
`energy_optimizer_battery_export_enabled` until the export characterization stage passes.
