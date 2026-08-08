from __future__ import annotations

import datetime as dt

from energy_optimizer.config import Settings
from energy_optimizer.watchdog import (
    FakeHaWatchdog,
    HeartbeatSample,
    HeartbeatSilenceTracker,
    heartbeat_is_healthy,
)


def _settings() -> Settings:
    return Settings(
        db=":memory:",
        mqtt_enabled=False,
        ha_token="",
        pstryk_api_key="",
        pv_forecast_provider="none",
        pv_planes=[],
        battery_capacity_kwh=10.0,
        battery_max_charge_kw=8.8,
        battery_max_discharge_kw=9.6,
        battery_control_max_charge_kw=8.8,
        battery_control_max_discharge_kw=9.6,
        battery_control_local_charge_limit_kw=8.8,
        battery_control_local_discharge_limit_kw=9.6,
        battery_control_local_charge_cutoff_pct=100.0,
        battery_control_local_discharge_cutoff_pct=0.0,
        battery_control_heartbeat_expiry_seconds=60.0,
        battery_soc_min_pct=20.0,
        battery_soc_max_pct=98.0,
        battery_round_trip_efficiency=0.90,
        degradation_cost_pln_per_kwh=0.05,
        site_import_limit_kw=14.0,
        site_export_limit_kw=14.0,
        inverter_limit_kw=12.0,
    )


def test_fresh_non_retained_heartbeat_is_healthy() -> None:
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    hb = HeartbeatSample(ts=now - dt.timedelta(seconds=10), retained=False)
    assert heartbeat_is_healthy(hb, now=now, expiry_seconds=60) is True


def test_expired_and_retained_heartbeats_are_unhealthy() -> None:
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    expired = HeartbeatSample(ts=now - dt.timedelta(seconds=61), retained=False)
    retained = HeartbeatSample(ts=now - dt.timedelta(seconds=1), retained=True, state="active")
    assert heartbeat_is_healthy(expired, now=now, expiry_seconds=60) is False
    assert heartbeat_is_healthy(retained, now=now, expiry_seconds=60) is False
    assert heartbeat_is_healthy(None, now=now, expiry_seconds=60) is False


def test_fake_watchdog_fallback_on_heartbeat_expiry_uses_configured_restores() -> None:
    settings = _settings()
    watchdog = FakeHaWatchdog(
        settings,
        displayed_number_values={
            settings.battery_control_charge_limit_entity: 0.0,
            settings.battery_control_discharge_limit_entity: 0.0,
        },
    )
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    decision = watchdog.maybe_act(
        heartbeat=HeartbeatSample(ts=now - dt.timedelta(seconds=90)),
        now=now,
    )
    assert decision.should_fallback is True
    assert decision.reason == "heartbeat_expired"
    assert ("switch", "turn_on") not in [(d, a) for d, a, _ in watchdog.service_calls]
    values = [
        call[2]["value"]
        for call in watchdog.service_calls
        if call[0] == "number" and call[1] == "set_value"
    ]
    assert values == [8.8, 9.6, 100.0, 0.0]
    assert (
        "switch",
        "turn_off",
        {"entity_id": settings.battery_control_remote_ems_switch_entity},
    ) in watchdog.service_calls


def test_retained_active_heartbeat_cannot_look_healthy() -> None:
    settings = _settings()
    watchdog = FakeHaWatchdog(settings)
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    decision = watchdog.maybe_act(
        heartbeat=HeartbeatSample(ts=now, retained=True, state="active"),
        now=now,
    )
    assert decision.healthy is False
    assert decision.reason == "retained_heartbeat_untrusted"
    assert decision.should_fallback is True


def test_mqtt_loss_and_lockout_and_emergency_trigger_fallback() -> None:
    settings = _settings()
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    fresh = HeartbeatSample(ts=now - dt.timedelta(seconds=5))

    for kwargs in (
        {"mqtt_available": False},
        {"controller_lockout": True},
        {"emergency_off": True},
    ):
        watchdog = FakeHaWatchdog(settings)
        decision = watchdog.maybe_act(heartbeat=fresh, now=now, **kwargs)
        assert decision.should_fallback is True
        assert ("switch", "turn_on") not in [(d, a) for d, a, _ in watchdog.service_calls]
        assert any(c[1] == "turn_off" for c in watchdog.service_calls)


def test_startup_with_remote_ems_on_falls_back_and_never_enables_remote() -> None:
    settings = _settings()
    watchdog = FakeHaWatchdog(settings)
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    decision = watchdog.maybe_act(
        heartbeat=HeartbeatSample(ts=now),
        now=now,
        remote_ems_on=True,
        startup=True,
    )
    assert decision.reason == "startup_remote_ems_on"
    assert all(not (d == "switch" and a == "turn_on") for d, a, _ in watchdog.service_calls)
    assert any(
        d == "switch"
        and a == "turn_off"
        and c["entity_id"] == settings.battery_control_remote_ems_switch_entity
        for d, a, c in watchdog.service_calls
    )


def test_healthy_path_issues_no_writes() -> None:
    settings = _settings()
    watchdog = FakeHaWatchdog(settings)
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    decision = watchdog.maybe_act(
        heartbeat=HeartbeatSample(ts=now - dt.timedelta(seconds=5)),
        now=now,
    )
    assert decision.healthy is True
    assert watchdog.service_calls == []


def test_silence_tracker_detects_expiry_without_new_mqtt_message() -> None:
    """Timer-tick evaluation with incoming=None must still expire a quiet heartbeat."""
    tracker = HeartbeatSilenceTracker()
    t0 = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    assert tracker.ingest(HeartbeatSample(ts=t0, retained=False), now=t0) is True

    # No further MQTT payloads — only a periodic evaluation.
    later = t0 + dt.timedelta(seconds=30)
    assert tracker.evaluate_silence(now=later, expiry_seconds=60, incoming=None).healthy is True

    expired = t0 + dt.timedelta(seconds=61)
    decision = tracker.evaluate_silence(now=expired, expiry_seconds=60, incoming=None)
    assert decision.healthy is False
    assert decision.reason == "heartbeat_expired"
    assert decision.should_fallback is True


def test_silence_tracker_rejects_retained_future_and_malformed() -> None:
    tracker = HeartbeatSilenceTracker()
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    assert (
        tracker.ingest(HeartbeatSample(ts=now, retained=True, state="active"), now=now) is False
    )
    assert tracker.last_reject_reason == "retained_heartbeat_untrusted"
    assert tracker.last_valid_ts is None

    assert (
        tracker.ingest(
            HeartbeatSample(ts=now + dt.timedelta(seconds=5), retained=False), now=now
        )
        is False
    )
    assert tracker.last_reject_reason == "heartbeat_from_future"

    assert tracker.ingest(HeartbeatSample(ts=now, retained=False, state=""), now=now) is False
    assert tracker.last_reject_reason == "heartbeat_malformed"


def test_timer_tick_fallback_uses_persisted_timestamp_not_trigger_payload() -> None:
    settings = _settings()
    watchdog = FakeHaWatchdog(settings)
    tracker = HeartbeatSilenceTracker()
    t0 = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    tracker.ingest(HeartbeatSample(ts=t0), now=t0)

    # Simulate time_pattern tick with no MQTT payload after expiry.
    now = t0 + dt.timedelta(seconds=90)
    decision = tracker.evaluate_silence(now=now, expiry_seconds=60, incoming=None)
    assert decision.should_fallback is True
    acted = watchdog.maybe_act(heartbeat=tracker.as_sample(), now=now)
    assert acted.should_fallback is True
    assert ("switch", "turn_on") not in [(d, a) for d, a, _ in watchdog.service_calls]
    assert any(c[1] == "turn_off" for c in watchdog.service_calls)
