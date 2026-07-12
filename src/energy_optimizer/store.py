"""SQLite persistence via SQLAlchemy 2.0 (declarative).

Schema mirrors the design's storage section. ``runs`` + ``plan_steps`` form the audit
log; ``runs.solver_input`` is an immutable versioned snapshot whose SHA-256 detects
mutation. Timestamps are stored as timezone-aware UTC datetimes.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy import (
    Text as SAText,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


class Base(DeclarativeBase):
    pass


class Telemetry(Base):
    __tablename__ = "telemetry"

    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    soc_pct: Mapped[float | None] = mapped_column(Float)
    batt_charge_kw: Mapped[float | None] = mapped_column(Float)
    batt_discharge_kw: Mapped[float | None] = mapped_column(Float)
    pv_kw: Mapped[float | None] = mapped_column(Float)
    load_kw: Mapped[float | None] = mapped_column(Float)
    grid_import_kw: Mapped[float | None] = mapped_column(Float)
    grid_export_kw: Mapped[float | None] = mapped_column(Float)
    ems_mode: Mapped[str | None] = mapped_column(String(64))
    stale: Mapped[bool] = mapped_column(Boolean, default=False)


class Price(Base):
    __tablename__ = "prices"

    interval_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    tge: Mapped[float | None] = mapped_column(Float)
    service: Mapped[float | None] = mapped_column(Float)
    distribution: Mapped[float | None] = mapped_column(Float)
    excise: Mapped[float | None] = mapped_column(Float)
    vat: Mapped[float | None] = mapped_column(Float)
    base: Mapped[float | None] = mapped_column(Float)
    buy_gross: Mapped[float | None] = mapped_column(Float)
    full_price: Mapped[float | None] = mapped_column(Float)
    sell_gross: Mapped[float | None] = mapped_column(Float)
    is_cheap: Mapped[bool | None] = mapped_column(Boolean)
    is_expensive: Mapped[bool | None] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(16), default="api")  # api | forecast
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Forecast(Base):
    __tablename__ = "forecasts"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interval_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)  # pv|load|price_buy|price_sell
    value: Mapped[float] = mapped_column(Float)
    confidence: Mapped[str] = mapped_column(String(16), default="ok")


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    mode: Mapped[str] = mapped_column(String(16))
    horizon_hours: Mapped[float] = mapped_column(Float)
    known_price_hours: Mapped[float] = mapped_column(Float)
    input_state: Mapped[str | None] = mapped_column(SAText)
    solver_input: Mapped[str | None] = mapped_column(SAText)
    solver_input_schema: Mapped[str | None] = mapped_column(String(32))
    solver_input_sha256: Mapped[str | None] = mapped_column(String(64))
    objective_pln: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))  # ok|blocked|low_confidence
    reason: Mapped[str | None] = mapped_column(SAText)
    safety: Mapped[str | None] = mapped_column(SAText)
    solve_ms: Mapped[float | None] = mapped_column(Float)


class PlanStep(Base):
    __tablename__ = "plan_steps"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interval_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    dt_hours: Mapped[float] = mapped_column(Float)
    pv_to_load_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    pv_to_battery_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    pv_to_grid_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    grid_to_load_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    grid_to_battery_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    battery_to_load_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    battery_to_grid_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    curtail_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    soc_pct_end: Mapped[float] = mapped_column(Float, default=0.0)
    marginal_value: Mapped[float | None] = mapped_column(Float)


class DailyReport(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (UniqueConstraint("date", name="uq_daily_reports_date"),)

    date: Mapped[str] = mapped_column(String(10), primary_key=True)  # ISO date, local
    actual_cost_pln: Mapped[float | None] = mapped_column(Float)
    optimizer_sim_cost_pln: Mapped[float | None] = mapped_column(Float)
    pvonly_cost_pln: Mapped[float | None] = mapped_column(Float)
    selfcons_cost_pln: Mapped[float | None] = mapped_column(Float)
    missed_opportunity_pln: Mapped[float | None] = mapped_column(Float)
    actual_import_kwh: Mapped[float | None] = mapped_column(Float)
    actual_export_kwh: Mapped[float | None] = mapped_column(Float)
    battery_cycles: Mapped[float | None] = mapped_column(Float)
    degradation_cost_pln: Mapped[float | None] = mapped_column(Float)
    pv_forecast_mae_kwh: Mapped[float | None] = mapped_column(Float)
    load_forecast_mae_kwh: Mapped[float | None] = mapped_column(Float)
    forecast_error_cost_pln: Mapped[float | None] = mapped_column(Float)


class Store:
    """Owns the SQLAlchemy engine and session factory for a single SQLite database."""

    def __init__(self, db_path: str, echo: bool = False) -> None:
        self.db_path = db_path
        self.engine = _make_engine(db_path, echo=echo)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _make_engine(db_path: str, echo: bool = False) -> Engine:
    connect_args = {"check_same_thread": False, "timeout": 30}
    if db_path == ":memory:":
        # A shared in-memory DB must reuse a single connection or each session sees an
        # empty database. StaticPool keeps one connection alive for the engine's lifetime.
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            echo=echo,
            future=True,
            connect_args=connect_args,
            poolclass=StaticPool,
        )
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite+pysqlite:///{db_path}",
            echo=echo,
            future=True,
            connect_args=connect_args,
        )
    _apply_sqlite_pragmas(engine)
    return engine


def _apply_sqlite_pragmas(engine: Engine) -> None:
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
