# PvOpti roadmap

## Current state — 2026-08-16

PvOpti is deployed in `dry_run` and disarmed. Battery control, grid charging,
discharge and export remain disabled. The 2% operating reserve is distinct from
the 0% hard/BMS floor.

Completed evidence:
- attended 0.5 kW grid-charge A/B/A with HA, raw-register and physical evidence;
- bounded heartbeat, process-stop, integration-reload and HA-restart fallbacks;
- Pstryk-only seven-day reconciliation baseline (168 settled hours);
- HA readiness plus persisted acknowledgement guard, fail-closed by default;
- persistent lockout, one reconnect restore attempt and verified local-safe fallback;
- unverified reachable fallback locks out; persistent path-loss lockout remains visible.

## Charge-only release gates

1. Record and review a real HA-to-inverter path-loss/reconnection incident. The
   record must include the loss interval, command, HA/raw/physical state, battery,
   grid and SoC response, restoration result and financial impact when settled.
2. Restore the missing Sigen HA control entities, while keeping readiness false
   until Remote EMS is explicitly off, all required watchdog actors are enabled,
   and latches are clear.
3. Run an explicitly authorized, attended 0.5 kW controller interval. It remains
   charge-only; discharge and export stay disabled.
4. Observe normal plan changes, no-command periods, restart/reconnect behavior,
   and stale telemetry/price/forecast/plan blocking.
5. Validate policy accounting over further settled Pstryk intervals, then approve
   a separately time-limited charge-only autonomous trial.

## Later stages

Discharge and export each require their own attended characterization, accounting
rules and fallback evidence. Unattended battery control is not authorized.

Related: `docs/battery-control-watchdog-interface.md`, `docs/control-runbook.md`,
and `docs/plans/2026-08-08_001527-pvopti-actual-energy-control.md`.
