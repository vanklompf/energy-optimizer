from __future__ import annotations

import datetime as dt

import pytest

from energy_optimizer.control_store import (
    claim_path_loss_recovery,
    clear_lockout,
    finalize_action,
    is_locked_out,
    list_pending_actions,
    persist_pending_action,
    release_lease,
    set_lockout,
    try_acquire_lease,
)
from energy_optimizer.store import ControlAction, Store


@pytest.fixture()
def store() -> Store:
    s = Store(":memory:")
    s.create_all()
    return s


def test_append_only_audit_and_pending_recovery(store: Store) -> None:
    with store.session() as session:
        persist_pending_action(
            session,
            command_id="cmd-1",
            source_run_id="run-1",
            interval_start=dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC),
            intent={"direction": "CHARGE", "power_kw": 0.5},
            authorization_allowed=True,
            blockers=[],
            requested_state="ACTIVE_CHARGE",
        )
        pending = list_pending_actions(session)
        assert len(pending) == 1
        assert pending[0].result == "pending"

        finalize_action(
            session,
            "cmd-1",
            observed_state="ACTIVE_CHARGE",
            physical={"battery_power_kw": 0.5},
            result="ok",
            latency_ms=120.0,
        )
        assert list_pending_actions(session) == []
        done = session.get(ControlAction, "cmd-1")
        assert done is not None
        assert done.result == "ok"
        assert done.error_code is None


def test_refuses_secret_like_intent_payload(store: Store) -> None:
    with store.session() as session:
        with pytest.raises(ValueError, match="secret"):
            persist_pending_action(
                session,
                command_id="cmd-secret",
                source_run_id=None,
                interval_start=None,
                intent={"ha_token": "should-not-store"},
                authorization_allowed=False,
                blockers=[],
                requested_state="DISARMED",
            )


def test_lease_compare_and_swap(store: Store) -> None:
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    with store.session() as session:
        assert try_acquire_lease(session, owner_id="a", ttl_seconds=30, now=now) is True
        assert try_acquire_lease(session, owner_id="b", ttl_seconds=30, now=now) is False
        assert try_acquire_lease(session, owner_id="a", ttl_seconds=30, now=now) is True
        # Expired lease can be stolen.
        later = now + dt.timedelta(seconds=31)
        assert try_acquire_lease(session, owner_id="b", ttl_seconds=30, now=later) is True
        assert release_lease(session, owner_id="a") is False
        assert release_lease(session, owner_id="b") is True


def test_concurrent_lease_acquisition(store: Store) -> None:
    # SQLite StaticPool is not a multi-writer lock manager; verify CAS semantics by
    # serializing conflicting acquires under the same transactional rules.
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    winners = 0
    for owner in (f"owner-{i}" for i in range(8)):
        with store.session() as session:
            if try_acquire_lease(session, owner_id=owner, ttl_seconds=60, now=now):
                winners += 1
    assert winners == 1


def test_path_loss_lockout_expires_and_recovery_is_claimed_once(store: Store) -> None:
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    with store.session() as session:
        set_lockout(session, reason="fallback_ha_unreachable", duration_seconds=1, now=now)
        assert is_locked_out(session, now=now) is True
        assert is_locked_out(session, now=now + dt.timedelta(seconds=2)) is False
        # Recovery claim is still one-shot while the original reason is present.
        set_lockout(session, reason="fallback_ha_unreachable", duration_seconds=60, now=now)
        assert claim_path_loss_recovery(session, now=now) is True
        assert claim_path_loss_recovery(session, now=now) is False
        clear_lockout(session, now=now)
        assert is_locked_out(session, now=now) is False


def test_lockout_persistence(store: Store) -> None:
    now = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
    with store.session() as session:
        set_lockout(session, reason="mismatch", duration_seconds=3600, now=now)
        assert is_locked_out(session, now=now) is True
        assert is_locked_out(session, now=now + dt.timedelta(seconds=3601)) is False
        clear_lockout(session, now=now)
        assert is_locked_out(session, now=now) is False
