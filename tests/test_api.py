from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from energy_optimizer.config import Settings
from energy_optimizer.store import (
    EvControlStatus,
    EvPlanStep,
    EvTelemetry,
    PlanStep,
    Price,
    PstrykMeterInterval,
    Run,
    Telemetry,
)
from energy_optimizer.web import create_app


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings, run_scheduler=False)
    with TestClient(app) as c:
        yield c


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_status_empty(client: TestClient) -> None:
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "dry_run"
    assert body["control_enabled"] is False
    assert body["telemetry"] is None
    assert body["ev"] is None
    assert body["ev_control"]["enabled"] is False
    assert body["ev_control"]["relay_settle_seconds"] == 5.0
    assert body["ev_control"]["relay_verify_timeout_seconds"] == 30.0
    assert body["ev_control"]["relay_failure_backoff_minutes"] == 30
    assert body["ev_control"]["opportunistic_grid_allowed"] is False
    assert body["ev_control"]["forecast_surplus_factor"] == 0.8
    assert "battery reserve is protected" in body["ev_control"]["policy_explanation"]
    assert body["billing_meter"] is None


def test_status_exposes_latest_pstryk_billing_interval(client: TestClient) -> None:
    store = client.app.state.store
    start = dt.datetime.now(tz=dt.UTC).replace(minute=0, second=0, microsecond=0) - dt.timedelta(
        hours=1
    )
    with store.session() as session:
        session.add(
            PstrykMeterInterval(
                interval_start=start,
                interval_end=start + dt.timedelta(hours=1),
                import_kwh=0.053,
                export_kwh=0.149,
                balance_kwh=-0.096,
                source="pstryk",
            )
        )

    meter = client.get("/api/status").json()["billing_meter"]

    assert meter["source"] == "pstryk"
    assert meter["interval_start"] == start.isoformat()
    assert meter["import_kwh"] == 0.053
    assert meter["export_kwh"] == 0.149
    assert meter["settled"] is True


def test_status_exposes_vehicle_and_control_state(client: TestClient) -> None:
    store = client.app.state.store
    client.app.state.service.ev_charge_to_100_active = True
    now = dt.datetime.now(tz=dt.UTC)
    with store.session() as session:
        session.add(
            EvTelemetry(
                ts=now,
                soc_pct=83.0,
                charging_status="2",
                charging_active=False,
                switch_on=False,
                switch_changed=now,
                power_kw=0.0,
                fault=False,
                stale=False,
            )
        )
        session.add(
            EvControlStatus(
                key="current",
                ts=now,
                desired_on=False,
                planned_on=False,
                action="none",
                reason="optimiser deferred charging",
            )
        )

    body = client.get("/api/status").json()

    assert body["ev"]["soc_pct"] == 83.0
    assert body["ev"]["plugged_in"] is True
    assert body["ev_control"]["override_active"] is True
    assert body["ev_control"]["effective_target_soc_pct"] == 100.0
    assert body["ev_control"]["reason"] == "optimiser deferred charging"


def test_plan_empty(client: TestClient) -> None:
    resp = client.get("/api/plan")
    assert resp.status_code == 200
    assert resp.json() == {"run": None, "steps": []}


def test_plan_includes_ev_charging_energy(client: TestClient) -> None:
    store = client.app.state.store
    now = dt.datetime.now(tz=dt.UTC).replace(second=0, microsecond=0)
    with store.session() as session:
        session.add(
            Run(
                run_id="r1",
                ts=now,
                mode="dry_run",
                horizon_hours=48,
                known_price_hours=24,
                status="ok",
            )
        )
        session.add(PlanStep(run_id="r1", interval_start=now, dt_hours=0.25))
        session.add(
            EvPlanStep(
                run_id="r1",
                interval_start=now,
                charge_kwh=0.45,
                planned_on=True,
            )
        )

    body = client.get("/api/plan").json()

    assert body["steps"][0]["ev_charge_kwh"] == 0.45
    assert body["steps"][0]["ev_planned_on"] is True


def _seed(app_store, base: dt.datetime, *, include_meter: bool = True) -> None:
    with app_store.session() as session:
        for h in range(6):
            ts = base + dt.timedelta(hours=h)
            buy = 0.2 if h < 3 else 2.0
            session.add(
                Price(
                    interval_start=ts,
                    buy_gross=buy,
                    full_price=buy,
                    sell_gross=buy * 0.5,
                    source="api",
                )
            )
            for minute in (0, 15, 30, 45):
                session.add(
                    Telemetry(
                        ts=ts + dt.timedelta(minutes=minute),
                        soc_pct=50.0,
                        pv_kw=0.0,
                        load_kw=1.0,
                        grid_import_kw=1.0,
                        grid_export_kw=0.0,
                        batt_charge_kw=0.0,
                        batt_discharge_kw=0.0,
                        stale=False,
                    )
                )
            if include_meter:
                session.add(
                    PstrykMeterInterval(
                        interval_start=ts,
                        interval_end=ts + dt.timedelta(hours=1),
                        import_kwh=1.0,
                        export_kwh=0.0,
                        balance_kwh=1.0,
                        source="pstryk",
                    )
                )


def test_backtest_returns_comparison(client: TestClient, settings: Settings) -> None:
    store = client.app.state.store
    base = dt.datetime(2026, 7, 12, 0, 0, tzinfo=dt.UTC)
    _seed(store, base)
    resp = client.post(
        "/api/backtest",
        json={
            "start": base.isoformat(),
            "end": (base + dt.timedelta(hours=6)).isoformat(),
            "policies": ["pv_only", "self_consumption"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["intervals"] == 6
    assert body["settled_start"] == base.isoformat()
    assert body["settled_end"] == (base + dt.timedelta(hours=6)).isoformat()
    policies = {r["policy"] for r in body["results"]}
    assert "optimiser" in policies
    assert "self_consumption" in policies
    assert "actual_pstryk" in policies


def test_backtest_uses_pstryk_meter_as_authoritative_actual(client: TestClient) -> None:
    store = client.app.state.store
    base = dt.datetime(2026, 8, 7, 7, 0, tzinfo=dt.UTC)
    _seed(store, base, include_meter=False)
    with store.session() as session:
        for h in range(6):
            ts = base + dt.timedelta(hours=h)
            session.add(
                PstrykMeterInterval(
                    interval_start=ts,
                    interval_end=ts + dt.timedelta(hours=1),
                    import_kwh=0.1,
                    export_kwh=0.2,
                    balance_kwh=-0.1,
                    source="pstryk",
                )
            )

    body = client.post(
        "/api/backtest",
        json={
            "start": base.isoformat(),
            "end": (base + dt.timedelta(hours=6)).isoformat(),
            "policies": [],
        },
    ).json()

    actual = next(result for result in body["results"] if result["policy"] == "actual_pstryk")
    assert actual["import_kwh"] == 0.6
    assert actual["export_kwh"] == 1.2


def test_backtest_refuses_unsettled_range_instead_of_using_sigen(client: TestClient) -> None:
    store = client.app.state.store
    base = dt.datetime(2026, 8, 6, 0, 0, tzinfo=dt.UTC)
    _seed(store, base, include_meter=False)

    response = client.post(
        "/api/backtest",
        json={
            "start": base.isoformat(),
            "end": (base + dt.timedelta(hours=6)).isoformat(),
            "policies": [],
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "no settled Pstryk meter data in range"


def test_backtest_rejects_incomplete_live_telemetry(client: TestClient) -> None:
    store = client.app.state.store
    base = dt.datetime(2026, 8, 5, 0, 0, tzinfo=dt.UTC)
    _seed(store, base)
    with store.session() as session:
        session.execute(delete(Telemetry).where(Telemetry.ts > base))

    response = client.post(
        "/api/backtest",
        json={"start": base.isoformat(), "end": (base + dt.timedelta(hours=6)).isoformat()},
    )

    assert response.status_code == 422
    assert "incomplete live telemetry" in response.json()["detail"]


def test_backtest_rejects_internal_settlement_gap(client: TestClient) -> None:
    store = client.app.state.store
    base = dt.datetime(2026, 8, 4, 0, 0, tzinfo=dt.UTC)
    _seed(store, base)
    with store.session() as session:
        session.execute(
            delete(PstrykMeterInterval).where(
                PstrykMeterInterval.interval_start == base + dt.timedelta(hours=2)
            )
        )

    response = client.post(
        "/api/backtest",
        json={"start": base.isoformat(), "end": (base + dt.timedelta(hours=6)).isoformat()},
    )

    assert response.status_code == 422
    assert "settlement gap" in response.json()["detail"]


def test_backtest_rejects_missing_leading_or_trailing_settlement(client: TestClient) -> None:
    store = client.app.state.store
    base = dt.datetime(2026, 8, 3, 0, 0, tzinfo=dt.UTC)
    _seed(store, base)

    leading = client.post(
        "/api/backtest",
        json={
            "start": (base - dt.timedelta(hours=1)).isoformat(),
            "end": (base + dt.timedelta(hours=6)).isoformat(),
        },
    )
    trailing = client.post(
        "/api/backtest",
        json={
            "start": base.isoformat(),
            "end": (base + dt.timedelta(hours=7)).isoformat(),
        },
    )

    assert leading.status_code == 422
    assert "missing leading settlement" in leading.json()["detail"]
    assert trailing.status_code == 422
    assert "missing trailing settlement" in trailing.json()["detail"]


def test_backtest_rejects_missing_sell_price_and_boundary_soc(client: TestClient) -> None:
    store = client.app.state.store
    base = dt.datetime(2026, 8, 2, 0, 0, tzinfo=dt.UTC)
    _seed(store, base)
    with store.session() as session:
        price = session.get(Price, base)
        assert price is not None
        price.sell_gross = None

    missing_price = client.post(
        "/api/backtest",
        json={"start": base.isoformat(), "end": (base + dt.timedelta(hours=6)).isoformat()},
    )
    assert missing_price.status_code == 422
    assert "missing settled price" in missing_price.json()["detail"]

    with store.session() as session:
        price = session.get(Price, base)
        assert price is not None
        price.sell_gross = 0.1
        first_hour = (
            session.query(Telemetry)
            .filter(Telemetry.ts >= base)
            .filter(Telemetry.ts < base + dt.timedelta(hours=1))
            .all()
        )
        for row in first_hour:
            row.soc_pct = None

    missing_soc = client.post(
        "/api/backtest",
        json={"start": base.isoformat(), "end": (base + dt.timedelta(hours=6)).isoformat()},
    )
    assert missing_soc.status_code == 422
    assert "missing fresh battery SoC" in missing_soc.json()["detail"]


def test_backtest_no_data_404(client: TestClient) -> None:
    resp = client.post(
        "/api/backtest",
        json={"start": "2020-01-01T00:00:00Z", "end": "2020-01-02T00:00:00Z"},
    )
    assert resp.status_code == 404


def _seed_hours(app_store, end: dt.datetime, hours: int) -> None:
    with app_store.session() as session:
        for h in range(hours):
            ts = end - dt.timedelta(hours=h + 1)
            buy = 0.2 if ts.hour < 6 else 2.0
            session.add(
                Price(
                    interval_start=ts,
                    buy_gross=buy,
                    full_price=buy,
                    sell_gross=buy * 0.5,
                    source="api",
                )
            )
            for minute in (0, 15, 30, 45):
                session.add(
                    Telemetry(
                        ts=ts + dt.timedelta(minutes=minute),
                        soc_pct=50.0,
                        pv_kw=0.0,
                        load_kw=1.0,
                        grid_import_kw=1.0,
                        grid_export_kw=0.0,
                        batt_charge_kw=0.0,
                        batt_discharge_kw=0.0,
                        stale=False,
                    )
                )
            session.add(
                PstrykMeterInterval(
                    interval_start=ts,
                    interval_end=ts + dt.timedelta(hours=1),
                    import_kwh=1.0,
                    export_kwh=0.0,
                    balance_kwh=1.0,
                    source="pstryk",
                )
            )


def test_savings_windows(client: TestClient) -> None:
    store = client.app.state.store
    end = dt.datetime.now(tz=dt.UTC).replace(minute=0, second=0, microsecond=0)
    # Cover the full trailing week plus today so both windows have data regardless
    # of what local time the test runs at.
    _seed_hours(store, end, 8 * 24)
    resp = client.get("/api/savings")
    assert resp.status_code == 200
    body = resp.json()
    for window in ("day", "week"):
        assert window in body
        w = body[window]
        assert w["intervals"] > 0
        assert w["actual_cost_pln"] is not None
        assert w["optimiser_cost_pln"] is not None
        assert w["savings_pln"] is not None
        # Optimiser is never worse than the measured actual, so savings >= 0.
        assert w["savings_pln"] >= -1e-6


def test_savings_empty(client: TestClient) -> None:
    resp = client.get("/api/savings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["day"]["intervals"] == 0
    assert body["day"]["savings_pln"] is None


def test_prices_window(client: TestClient) -> None:
    store = client.app.state.store
    now = dt.datetime.now(tz=dt.UTC)
    floor = now.replace(minute=0, second=0, microsecond=0)
    with store.session() as session:
        for h in range(-4, 5):
            session.add(
                Price(
                    interval_start=floor + dt.timedelta(hours=h),
                    buy_gross=1.0 + h * 0.1,
                    full_price=1.0 + h * 0.1,
                    sell_gross=0.5,
                    source="api",
                )
            )
    resp = client.get("/api/prices?past_hours=3&future_hours=3")
    assert resp.status_code == 200
    body = resp.json()
    # 3h past + current + 3h future = 7 hourly points within the requested window.
    assert len(body["prices"]) == 7
    assert body["current_hour"] is not None
    assert all("buy_gross" in p for p in body["prices"])
