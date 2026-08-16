# PvOpti application design

## Purpose and boundary

PvOpti optimizes household, PV, stationary-battery and Mercedes charging decisions.
It is a shadow planner by default. Battery actuation is a separately gated,
charge-only control plane; it must fail closed and never infer that a lost command
expires at the inverter.

## Components

- `service.py`: collects telemetry, prices, forecasts, plans and orchestrates
  battery control/fallback/reconnect handling.
- `battery_control.py`: converts the current plan interval into a bounded intent.
- `sigenergy_control.py`: performs the characterized HA command transaction and
  verifies acknowledgement plus physical response; fallback returns local EMS.
- `control_store.py`: SQLite lease, append-only actions and persistent lockout.
- `ha_client.py`: HA state/history reads and controlled service calls.
- `web/routes.py`: read-only status, plan, settled-data and audit surfaces.
- HA watchdog configuration: independently ingests heartbeat and is OFF-only;
  it never enables Remote EMS.

## Authorization model

Control requires all independent gates: `mode=control`, enabled control, exact arm
token, version-bound acknowledgement evidence, fresh plan/telemetry/price inputs,
lease ownership, a healthy HA readiness plus acknowledgement pair, and the
charge-only direction policy. Any missing, stale, malformed or unavailable input
blocks control. The configured HA guard additionally requires Remote EMS off
before it can claim readiness.

## Failure model

Fallback is the only plan-independent action. HA unreachable, failed acknowledgement,
physical verification failure or an unverified reachable restore lock out control.
Path-loss lockouts persist beyond their time field, permit one OFF-only reconnect
restore attempt, and require human review/re-arm even after verified restoration.

## Data and accounting

Pstryk settled hourly import/export is the billing authority. Inverter estimates
never fill missing settlement intervals. Historical HA telemetry is coverage-checked
before reconciliation and is backfilled read-only when any completed hour is
incomplete.

## Current operational state

The production deployment is dry-run/disarmed. HA readiness is unavailable because
the configured Sigen control entities are absent; this is intentionally fail-closed.
No discharge, export or unattended control is permitted.

See `docs/ROADMAP.md` for release gates and `docs/control-runbook.md` for the
operator procedure.
