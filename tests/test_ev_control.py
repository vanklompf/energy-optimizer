from __future__ import annotations

import datetime as dt

import pytest

from energy_optimizer.config import Settings
from energy_optimizer.ev_control import EvLiveState, decide_ev_control


def _settings() -> Settings:
    return Settings(
        db=":memory:",
        ev_control_enabled=True,
        ev_min_on_minutes=15,
        ev_min_off_minutes=5,
    )


def _state(now: dt.datetime, **overrides) -> EvLiveState:
    values = dict(
        soc_pct=40.0,
        charging_status="2",
        switch_on=False,
        switch_last_changed=now - dt.timedelta(minutes=10),
        power_kw=0.0,
        fault=False,
    )
    values.update(overrides)
    return EvLiveState(**values)


def test_controller_turns_on_for_planned_slot_after_off_cooldown() -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    decision = decide_ev_control(_settings(), _state(now), planned_on=True, now=now)
    assert decision.action == "turn_on"
    assert decision.desired_on is True


def test_controller_waits_for_minimum_on_time_before_stopping() -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    live = _state(
        now,
        switch_on=True,
        switch_last_changed=now - dt.timedelta(minutes=5),
        power_kw=1.8,
    )
    decision = decide_ev_control(_settings(), live, planned_on=False, now=now)
    assert decision.action == "none"
    assert "minimum on time" in decision.reason


def test_controller_fails_safe_off_when_car_is_unplugged() -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    live = _state(now, switch_on=True, charging_status="3")
    decision = decide_ev_control(_settings(), live, planned_on=True, now=now)
    assert decision.action == "turn_off"
    assert decision.desired_on is False


def test_controller_fails_safe_off_when_plan_is_missing() -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    live = _state(now, switch_on=True)
    decision = decide_ev_control(_settings(), live, planned_on=None, now=now)
    assert decision.action == "turn_off"
    assert "plan" in decision.reason


def test_controller_stops_immediately_at_target() -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    live = _state(now, switch_on=True, soc_pct=100.0, switch_last_changed=now)
    decision = decide_ev_control(
        _settings(), live, planned_on=True, now=now, force_charge=True
    )
    assert decision.action == "turn_off"
    assert "target SoC reached" in decision.reason


def test_immediate_override_forces_charging_ignoring_plan() -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    decision = decide_ev_control(
        _settings(),
        _state(now, soc_pct=83.0),
        planned_on=False,
        now=now,
        force_charge=True,
    )

    assert decision.desired_on is True
    assert decision.action == "turn_on"
    assert "immediate charging override" in decision.reason


@pytest.mark.parametrize("status", ["3", "4", "16", "99", "malformed"])
def test_unverified_mercedes_status_never_permits_relay_on(status: str) -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    decision = decide_ev_control(
        _settings(), _state(now, charging_status=status), planned_on=True, now=now
    )

    assert decision.desired_on is False
    assert decision.action == "none"
    assert "not safe" in decision.reason or "unplugged" in decision.reason


@pytest.mark.parametrize("status", ["4", "16", "99", "malformed"])
def test_unverified_mercedes_status_forces_relay_off(status: str) -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    decision = decide_ev_control(
        _settings(),
        _state(now, charging_status=status, switch_on=True),
        planned_on=True,
        now=now,
    )

    assert decision.desired_on is False
    assert decision.action == "turn_off"


def test_verified_connected_status_permits_planned_start() -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    decision = decide_ev_control(
        _settings(), _state(now, charging_status="2"), planned_on=True, now=now
    )
    assert decision.action == "turn_on"


@pytest.mark.parametrize("status", ["0", "1", "5", "6", "9", "10", "11", "12", "13", "14", "15"])
def test_verified_active_status_keeps_powered_charger_on(status: str) -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    decision = decide_ev_control(
        _settings(),
        _state(now, charging_status=status, switch_on=True, power_kw=1.8),
        planned_on=True,
        now=now,
    )
    assert decision.desired_on is True
    assert decision.action == "none"


def test_unknown_switch_state_attempts_idempotent_off() -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    decision = decide_ev_control(
        _settings(), _state(now, switch_on=None), planned_on=False, now=now
    )

    assert decision.desired_on is False
    assert decision.action == "turn_off"


def test_relay_on_without_charging_power_after_grace_period_forces_off() -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    decision = decide_ev_control(
        _settings(),
        _state(
            now,
            switch_on=True,
            switch_last_changed=now - dt.timedelta(minutes=10),
            power_kw=0.0,
        ),
        planned_on=True,
        now=now,
    )

    assert decision.action == "turn_off"
    assert "no charging power" in decision.reason


def test_relay_on_without_power_during_startup_grace_remains_on() -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    decision = decide_ev_control(
        _settings(),
        _state(
            now,
            switch_on=True,
            switch_last_changed=now - dt.timedelta(minutes=2),
            power_kw=0.0,
        ),
        planned_on=True,
        now=now,
    )

    assert decision.action == "none"


def test_relay_on_without_last_changed_fails_closed() -> None:
    now = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)
    decision = decide_ev_control(
        _settings(),
        _state(now, switch_on=True, switch_last_changed=None, power_kw=0.0),
        planned_on=True,
        now=now,
    )

    assert decision.action == "turn_off"
    assert "timestamp" in decision.reason
