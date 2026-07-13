from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from energy_optimizer.config import Settings
from energy_optimizer.store import Price, Telemetry
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


def test_plan_empty(client: TestClient) -> None:
    resp = client.get("/api/plan")
    assert resp.status_code == 200
    assert resp.json() == {"run": None, "steps": []}


def _seed(app_store, base: dt.datetime) -> None:
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
            session.add(
                Telemetry(
                    ts=ts,
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
    policies = {r["policy"] for r in body["results"]}
    assert "optimiser" in policies
    assert "self_consumption" in policies
    assert "actual_sigen" in policies


def test_backtest_no_data_404(client: TestClient) -> None:
    resp = client.post(
        "/api/backtest",
        json={"start": "2020-01-01T00:00:00Z", "end": "2020-01-02T00:00:00Z"},
    )
    assert resp.status_code == 404


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
