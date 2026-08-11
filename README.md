# energy-optimizer

Solar + battery + optional EV/PHEV optimisation for a Sigen system on Pstryk dynamic
pricing. Sigen control stays **dry-run only** (recommendations via MQTT). An opt-in
controller can switch a fixed-power charger through Home Assistant with fail-safe OFF
and anti-cycling delays.

Settled Pstryk meter values are the sole source for billed import/export, savings,
backtests, and historical load calibration. PvOpti deliberately does not fall back to
Sigen grid counters when Pstryk has not settled an interval. Sigen remains in use only
for live behind-the-meter power, PV, battery, and SoC telemetry needed by the controller.
Counterfactuals require contiguous hourly settlement and price data plus coverage-checked,
time-integrated PV and battery telemetry; incomplete hours are reported instead of being
zero-filled or compressed out of time.

See [`DESIGN.md`](./DESIGN.md) for the design. Config reference: [`.env.example`](./.env.example).

Battery-control implementation handoff:

- [implementation plan](./docs/plans/2026-08-08_001527-pvopti-actual-energy-control.md)
- [site and external-system context](./docs/battery-control-context.md)
- [verified Sigenergy control contract](./docs/sigenergy-control-contract.md)
- [deployment variable contract](./docs/deployment-variables.md)
- [watchdog interface](./docs/battery-control-watchdog-interface.md)
- [live-control operator runbook](./docs/control-runbook.md)

The implementation plan is non-actuating: ordinary coding, tests, and CI must not contact or control the live inverter.

## Docker-first

Run only in Docker. Common tasks are in the `Makefile` and compose files:

- `compose.yml` — production-style run
- `compose.dev.yml` — hot-reload API + tests/lint

Multi-stage `Dockerfile` targets: `frontend`, `python-base`, `dev`, `runtime`. The SPA
is baked into the runtime image.

```bash
cp .env.example .env   # HA token, Pstryk key, MQTT, PV/site limits
docker compose up -d --build
```

| URL | Purpose |
|---|---|
| http://localhost:8320/ | SPA |
| http://localhost:8320/healthz | Liveness |
| http://localhost:8320/api/ | REST |
| http://localhost:8320/auth/login | OIDC login (when enabled) |

Local development leaves OIDC off. Production on the NAS uses Authentik
(`OIDC_ENABLED`, `APP_URL`, `SESSION_SECRET`, … — see `.env.example`).

SQLite state lives in `./data` → `/data`.

```bash
docker compose logs -f
docker compose down
```

## Develop

```bash
docker compose -f compose.dev.yml up --build              # API :8320
docker compose -f compose.dev.yml --profile frontend up frontend  # Vite :5173
make test        # pytest
make lint        # ruff
make typecheck   # mypy
make shell
```

## Configuration

All settings use the `EO_` env prefix (see `.env.example`). Required for real use:
`EO_HA_TOKEN`, `EO_PSTRYK_API_KEY`, MQTT credentials, PV planes, and site/inverter limits.

EV control (`EO_EV_CONTROL_ENABLED`) is off by default. When on, the optimiser guarantees
`EO_EV_MINIMUM_TARGET_SOC_PCT` by `EO_EV_DEPARTURE_HOUR`, then prefers solar/cheap grid
slots. Relay OFF for unplugged/unavailable/fault; `EO_EV_CHARGE_TO_100_ENTITY` forces
immediate full charge when ON.

Stationary battery control defaults to non-actuating: `EO_MODE=dry_run`,
`EO_BATTERY_CONTROL_ENABLED=false`, empty `EO_BATTERY_CONTROL_ARM_TOKEN`, and
`EO_BATTERY_EXPORT_ENABLED=false`. `EO_MODE=control` also requires the documented arm
token, reliable number-register acknowledgement, and the entity/timing validation in
`.env.example`. Planner flags `EO_ALLOW_GRID_CHARGING` / `EO_ALLOW_BATTERY_EXPORT` do
not arm physical battery control. See the Sigenergy control contract before changing
any of these.

## Deployment

ansible-nas role `energy_optimizer` (GHCR pull or local build from this tree). Tag push
publishes to GHCR:

```bash
git tag v0.1.0 && git push origin v0.1.0
# ghcr.io/vanklompf/energy-optimizer:latest and :v0.1.0
EO_IMAGE=ghcr.io/vanklompf/energy-optimizer:latest docker compose up -d
```

## License

MIT
