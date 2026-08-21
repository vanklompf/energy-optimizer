# Running PvOpti live

How to keep PvOpti in live control and how to get from a daily glance to a
weekly one. This is the current operating document;
[`commissioning/README.md`](./commissioning/README.md) is the Stage 1 attended
runbook and is now history.

Live control stays armed. The planner picks directions on its own. Nothing
needs restoring at the end of a day. The destination is unattended operation
(weekly review); daily review is only how you notice whether that is already
true.

## Going live

Set the gates in `HpeNas/group_vars/nas/main.yml` and deploy. These are the only
values that change from the dry-run posture:

```yaml
energy_optimizer_mode: control
energy_optimizer_battery_control_enabled: true
energy_optimizer_battery_control_grid_charge_enabled: true
energy_optimizer_battery_control_authorize_discharge: true
energy_optimizer_battery_export_enabled: true
energy_optimizer_battery_control_supported_directions: '["FALLBACK","IDLE","CHARGE","DISCHARGE"]'
```

`EXPORT` is not a direction; export is `DISCHARGE` plus
`energy_optimizer_battery_export_enabled`. The ansible-nas role refuses to deploy
`control` without the enable flag and both watchdog entity IDs, refuses
`authorize_discharge` without `DISCHARGE` in the supported directions, and
refuses export without discharge, so a half-configured inventory fails at deploy
time rather than at the inverter.

```bash
/mnt/nas/media/code/HomeLab/AnsibleNasConfigs/.cursor/skills/deploy-ansible-nas/deploy-ansible-nas.sh HpeNas energy_optimizer
```

Before the first live deploy, confirm the Sigen integration has `read_only:
false`, all six control entities are enabled in the entity registry, both
watchdog automations are on with `initial_state: true`, and
`input_boolean.pvopti_battery_control_emergency_off` and the fallback latch are
off. `/api/status` must report `watchdog_healthy`. After that, leave them that
way.

The operating reserve is **2%** on HpeNas
(`energy_optimizer_battery_soc_min_pct` and
`energy_optimizer_battery_control_min_soc_pct`). That is site policy, not a
commissioning relaxation: the BMS owns equipment safety. Do not raise it as a
precondition for unattended operation.

## Daily review, then stop

Once a day until it is boring, then weekly. Everything below is visible from
the web UI and `/api/status`; none of it requires being at the site, and none
of it is a reason to disarm.

Look at the Savings view first and reconcile plan against Pstryk settled
values. Settled data is the only accepted source for billed import and export,
so a gap between planned and settled is either a modelling error worth fixing
or a control failure worth reading the audit log about.

Then read the control action audit log for the day. Failures will appear; what
matters is that each one recovered on its own. Expect stale-input blocking, the
occasional verification failure followed by fallback, and lockouts that expire
after their cooldown. What would be wrong is a lockout that never clears, a
fallback that left Remote EMS on, limits restored to `0.0`, or the same failure
reason repeating every cycle for hours.

Watch for these specifically at first, since each one is new under continuous
operation:

- **Direction changes the planner makes on its own**, especially charge into
  discharge across a price boundary. Reversals pass through Standby-neutral, so a
  reversal that cannot reach the 0.12 kW band will fall back.
- **Export in daylight.** The controller picks `PV First` whenever measured PV
  exceeds `EO_BATTERY_CONTROL_EXPORT_PV_THRESHOLD_KW` and `ESS First` otherwise,
  because ESS First curtails PV to zero at any discharge limit. Confirm from
  telemetry that a daylight export keeps PV producing. An export that zeroes PV
  means the mode selection did not take.
- **Behavior at 100% SoC.** A pinned SoC no longer fails verification while it has
  headroom above the discharge cut-off.
- **The horizon edge.** Stored energy is valued at the cheapest import the
  horizon can see, so the plan should not drift toward emptying the battery
  before Pstryk publishes.

## Residual risk

**Modbus or network path loss is not contained, and that is accepted.**
Measured 2026-08-18: the inverter held the last Remote EMS command for the full
90 s outage, HA served cached Sigen entities so the watchdog never saw a fault,
and state A was only restored by an attended write. PvOpti itself failed closed
and did not issue new commands. There is no characterized inverter-native
timeout.

Accepted because the exposure is bounded: the inverter holds a command PvOpti
already verified as safe, within limits the BMS enforces independently.
Closing it later needs either an inverter-side timeout or a watchdog that
detects cached-but-frozen HA entities rather than only `unavailable` ones. Do
not re-run the fault; it is characterized. It is not a reason to stay attended.

**Full-rate import charging above ~6.9 kW is uncharacterized.** Sub-test 1d
plateaued: the battery held 6.36 kW against a 6.86 kW command and never reached
8.8 kW. Commands verify and the site import cap was never approached, so this
is waived. A plan that calls for maximum-rate charging will simply deliver less
than it asked for.

## Aborting

The operator override is:

```bash
docker stop energy-optimizer
```

An intentional stop remains stopped because the container uses
`restart: unless-stopped`. Graceful shutdown performs a verified fallback before
exit; if the process is hung or must be killed, heartbeat expiry makes the
independent HA watchdog restore local control. The app does not stop its own
container and has no Docker-socket access.

Do not use an HA Remote EMS change or an in-process API toggle as the override:
either can be replaced by a later control cycle or restart. Start the container
again only after review; startup reconciliation leaves unexpected Remote EMS
ownership before normal authorization can resume.

Aborting is for a control fault, not for an expensive hour. Economic mistakes are
expected and are not a reason to intervene.

## Unattended (Stage 3)

Stage 3 is the same configuration with a weekly review instead of a daily one.
Move as soon as a few consecutive days armed look right: fallbacks restore
local control on their own, lockouts expire, daylight export keeps PV on, and
settled Pstryk is close enough to the plan that you would rather tune later
than watch more closely now.

Tuning belongs after that, not before. Leave `minimum_export_spread_pln_kwh`,
`grid_charge_margin_pln_kwh`, and the activation margin alone until enough
settled data describes one configuration.
