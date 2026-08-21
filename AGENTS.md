# Repository Guidance

## Safety Boundary

PvOpti is moving to **unattended live operation**. Write code and docs for a
system that stays in `EO_MODE=control` with grid charge, discharge, and export
enabled. That is the normal state of this repo, not an exception that needs
justifying, restoring, or recording.

There is no per-window checkpoint limit, no requirement to restore
`read_only: true` or the non-actuating gates at the end of a session, and no
need to record a dated note for ordinary operation. Notes are for
characterizing new behavior, not for running. Follow
[`docs/OPERATIONS.md`](docs/OPERATIONS.md) for going live, aborting, and the
path from daily review into weekly (unattended) review.

Changes should reduce supervision, not add it. Do not add gates, ceremony, arm
tokens, or attended-only restrictions. If a failure mode needs containing,
contain it in code that recovers on its own.

- Ordinary development, tests, and CI remain non-actuating. Use the HA
  emulator/fakes and dummy credentials. Do not contact the live inverter from
  pytest, `make test`, or a local Compose stack.
- Deploy only via ansible-nas to HpeNas; never start a second Compose stack
  against the live site. `tools/sigen_raw_diagnostic.py` reads only and is the
  independent baseline whenever HA telemetry is in doubt.
- Planner options such as `EO_ALLOW_GRID_CHARGING` and `EO_ALLOW_BATTERY_EXPORT`
  do not authorize physical control. Actuation still requires `EO_MODE=control`,
  `EO_BATTERY_CONTROL_ENABLED`, and the per-direction gate.
- Compose defaults `EO_MODE` to `dry_run` via `${EO_MODE:-dry_run}` so a fresh
  checkout is inert; the deployed HpeNas inventory is what sets the live mode.
- The OFF-only watchdog stays. It is the one mechanism that must keep working:
  if the app dies, the inverter returns to local self-consumption.
- Before changing battery-control behavior or configuration gates, read
  `docs/DESIGN.md`, `docs/ROADMAP.md`, and `docs/sigenergy-control-contract.md`.
  Older handoff docs (`battery-control-context.md`, `control-runbook.md`, etc.)
  now live in `docs/archive/` and are historical only.
- Dated notes under `docs/commissioning/` are evidence from the attended Stage 1
  windows. They record what was true on their date and are not current
  operating policy.

## Workflow

- Use Docker/Compose v2 and the root `Makefile`; the supported workflow does not require host Python or Node. Python is 3.12 and frontend builds use Node 20.
- Run the local gate in this order: tests, `make lint`, `make typecheck`, `make fe-build`. The Make targets rebuild the `dev` image; CI currently treats mypy as non-blocking, but local changes should still pass it.
- `make test` currently leaves two `tests/test_sigen_raw_diagnostic.py` failures because the dev image omits `tools/`. Run the complete non-actuating suite with `make build-dev`, then `docker compose -f compose.dev.yml run --rm --no-deps -v "$PWD/tools:/app/tools:ro" app pytest -q`; this only makes the script importable by its tests. Do not execute the diagnostic from pytest. It is the independent baseline whenever HA telemetry is in doubt.
- Run one test without starting dependencies:
  `docker compose -f compose.dev.yml run --rm --no-deps app pytest tests/test_config.py::test_battery_control_defaults_are_non_actuating -v`
- Run the API at `http://localhost:8320` with `make dev`. Run the optional Vite server at `http://localhost:5173` with `docker compose -f compose.dev.yml --profile frontend up frontend`.
- `make up` runs the production-style dry-run stack. Copy `.env.example` to `.env` only when the application needs local integration settings; tests do not require it.
- Do not substitute lockfile-oriented commands without updating the build definitions: Docker currently installs Python with `pip install`, and frontend builds intentionally run `npm install`, not `uv sync` or `npm ci`.

## Architecture

- This is one `src`-layout Python package (`src/energy_optimizer`) plus a separate React/Vite package (`frontend`). Production starts `python -m energy_optimizer` with one Uvicorn worker because the process owns an in-process APScheduler.
- `service.py` is the orchestration center; `scheduler.py` wires startup and periodic jobs; `optimiser.py` owns the MILP; `store.py` and `control_store.py` own SQLite persistence; `web/` owns FastAPI, OIDC, REST, and SPA serving.
- Vite writes generated assets directly to `src/energy_optimizer/web/static`; that directory and generated Vite JS/declaration files are ignored. Edit `frontend/vite.config.ts`, not generated `vite.config.js` or `.d.ts` files.
- SQLite is the only datastore (`./data` mounted at `/data`). Startup uses SQLAlchemy `create_all()`; there is no active migration flow, so schema changes need explicit compatibility handling rather than assuming Alembic runs.
- Settled Pstryk values are the sole source for billed import/export, savings, backtests, and historical load calibration. Do not silently substitute Sigen grid counters or fill incomplete settlement/telemetry intervals.
- Settings normally use the `EO_` prefix, but OIDC variables (`OIDC_ENABLED`, `APP_URL`, `SESSION_SECRET`, etc.) are read directly without it. `.env.example` is useful setup guidance, not a complete inventory of every `Settings` field.

## Tests And Generated State

- Pytest uses automatic asyncio mode. Shared fixtures provide in-memory SQLite with MQTT and external credentials disabled; API tests create the app with `run_scheduler=False`.
- Battery "integration" tests are non-network fault-injection tests built on `tests/ha_emulator.py`. Some deliberately use `Settings.model_construct()` to bypass validation; those objects are not examples of deployable configuration.
- Keep generated/cached state out of commits: `frontend/node_modules`, `src/energy_optimizer/web/static`, TypeScript build-info/generated Vite files, Python caches, and local `data/` contents.
- Historical implementation-plan status tables are dated snapshots and contain stale "not implemented" entries. Trust current code, Compose/Docker definitions, CI, and tests over those status tables or conflicting prose.
