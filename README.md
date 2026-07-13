# energy-optimizer

Solar + battery optimisation app for a Sigen PV/battery system on Pstryk dynamic pricing.
Runs **dry-run first**: it collects telemetry (Home Assistant) and prices (Pstryk), plans
battery/grid flows with a duration-aware explicit-flow MILP, and publishes *recommendations*
to Home Assistant over MQTT. It never controls hardware in the MVP (`control_enabled` is
hardcoded off).

See [`DESIGN.md`](./DESIGN.md) for the full design.

## Docker-first

This app is **only ever run in Docker** — there is no supported host-Python workflow. All
common tasks are wrapped in the `Makefile` and the two compose files:

- `compose.yml` — production-style run (dry_run).
- `compose.dev.yml` — hot-reloading dev server + a container for tests/lint.

The image is multi-stage (`Dockerfile`) with targets `frontend`, `python-base`, `dev` and
`runtime`. The SPA is built inside the image; there is no separate frontend server in
production.

## Run it

```bash
cp .env.example .env        # then fill in HA token, Pstryk key, MQTT creds, PV/site limits
docker compose up -d --build
```

- SPA + dashboards: <http://localhost:8320/>
- Liveness (used by the Docker healthcheck): <http://localhost:8320/healthz>
- REST API: under <http://localhost:8320/api/>

State (the SQLite DB) is persisted to `./data` (mounted at `/data`).

```bash
docker compose logs -f      # tail logs
docker compose down         # stop
```

## Develop

```bash
cp .env.example .env
docker compose -f compose.dev.yml up --build     # hot-reloading API on :8320
```

Source is bind-mounted, so edits reload automatically. For the SPA dev server with live
proxy to the backend:

```bash
docker compose -f compose.dev.yml --profile frontend up frontend   # Vite on :5173
```

### Tests, lint, type-check (all in Docker)

```bash
make test        # pytest inside the dev image
make lint        # ruff
make typecheck   # mypy
make shell       # a shell in the dev image
```

Equivalently, without make:

```bash
docker compose -f compose.dev.yml run --rm --no-deps app pytest -q
docker compose -f compose.dev.yml run --rm --no-deps app ruff check src tests
```

## Configuration

All configuration is via environment variables (prefix `EO_`) or the `.env` file. See
[`.env.example`](./.env.example) for the full list with defaults and comments. Required
before real use: `EO_HA_TOKEN`, `EO_PSTRYK_API_KEY`, MQTT credentials, and the PV-plane /
site grid / inverter limits.

## Deployment

Production deployment is via the ansible-nas `energy_optimizer` role, which pulls the image
from GHCR (no local build). Tagged releases are published automatically by GitHub Actions
(see [basen](https://github.com/vanklompf/basen) for the same pattern):

```bash
git tag v0.1.0
git push origin v0.1.0
```

This builds and pushes to GHCR with tags `latest` and `v0.1.0`:

- `ghcr.io/vanklompf/energy-optimizer:latest`
- `ghcr.io/vanklompf/energy-optimizer:v0.1.0`

To run a GHCR image with compose directly:

```bash
EO_IMAGE=ghcr.io/vanklompf/energy-optimizer:latest docker compose up -d
```

## Status

Early development. Implemented so far:

- Phase 1 (data spine): config, SQLite store, Pstryk unified-metrics client, HA client,
  collector scheduler jobs, `/api/status`, `/healthz`.
- Phase 2 (optimiser + simulator): explicit-flow duration-aware MILP (HiGHS via PuLP),
  baseline policies, replay simulator, accounting, `POST /api/backtest`.
- Scaffolding for forecasts, safety, explainability, MQTT publishing and the SPA.

## License

MIT
