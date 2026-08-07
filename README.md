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
