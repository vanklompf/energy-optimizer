# energy-optimizer

Single-site home energy manager for a Sigen PV + battery system with optional EV/PHEV
charging, on the Pstryk dynamic-pricing tariff. A MILP optimiser plans battery
charge/discharge, grid import/export, and EV charging to minimise the electricity bill.

The full battery-control stack is implemented but **actuation is gated by `EO_MODE`**:
it defaults to `dry_run` (plan + MQTT recommendations, no inverter writes). Setting
`EO_MODE=control` actuates the plan through Home Assistant (Sigen Remote EMS), subject
to a small fail-closed safety layer. EV relay control is a separate opt-in
(`EO_EV_CONTROL_ENABLED`) with fail-safe OFF and anti-cycling delays.

Settled Pstryk meter values are the sole source for billed import/export, savings,
backtests, and historical load calibration. PvOpti deliberately does not fall back to
Sigen grid counters when Pstryk has not settled an interval. Sigen is used only for
live behind-the-meter power, PV, battery, and SoC telemetry. Counterfactuals require
contiguous hourly settlement and price data plus coverage-checked, time-integrated
telemetry; incomplete hours are reported instead of being zero-filled.

Documentation:

- [design](./docs/DESIGN.md) — architecture, data flows, optimiser, safety model
- [roadmap to production](./docs/ROADMAP.md) — assumptions, simplification plan, staged go-live
- [verified Sigenergy control contract](./docs/sigenergy-control-contract.md) — empirical inverter/HA behavior
- [`.env.example`](./.env.example) — configuration reference
- [`docs/archive/`](./docs/archive/) — superseded plans/designs, kept for history only

Ordinary coding, tests, and CI are non-actuating: they must not contact or control the
live inverter (use the HA emulator/fakes).

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

Stationary battery control defaults to non-actuating: `EO_MODE=dry_run` and
`EO_BATTERY_CONTROL_ENABLED=false`. Live actuation needs `EO_MODE=control` and
`EO_BATTERY_CONTROL_ENABLED=true`, plus the direction flags for the behaviors you want
(`EO_BATTERY_CONTROL_GRID_CHARGE_ENABLED`, `EO_BATTERY_CONTROL_AUTHORIZE_DISCHARGE`,
`EO_BATTERY_EXPORT_ENABLED`), and valid entity/limit/timing config. The planner flags
`EO_ALLOW_GRID_CHARGING` / `EO_ALLOW_BATTERY_EXPORT` only affect what the plan may
recommend; they do not by themselves actuate the inverter. Compose reads `EO_MODE`
from `.env` and defaults to `dry_run`, so a fresh checkout is inert; the deployed
HpeNas inventory is what sets the live mode. The site is moving to unattended
live control — see [operations](./docs/OPERATIONS.md) for the gate values and
review cadence, the [roadmap](./docs/ROADMAP.md) for where that sits, and the
[Sigenergy control contract](./docs/sigenergy-control-contract.md) for what is
and is not physically characterized.

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
