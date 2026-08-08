"""Battery controller persistence helpers: audit, lease CAS, and lockout state."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import asdict, is_dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .store import ControlAction, ControllerLease, ControllerStateRow, utcnow

DEFAULT_SITE_KEY = "sigen_plant"
CONTROLLER_STATE_KEY = "current"


def ensure_controller_state(session: Session) -> ControllerStateRow:
    row = session.get(ControllerStateRow, CONTROLLER_STATE_KEY)
    if row is None:
        row = ControllerStateRow(key=CONTROLLER_STATE_KEY, state="DISARMED")
        session.add(row)
        session.flush()
    return row


def persist_pending_action(
    session: Session,
    *,
    command_id: str,
    source_run_id: str | None,
    interval_start: dt.datetime | None,
    intent: object | None,
    authorization_allowed: bool,
    blockers: list[str] | tuple[str, ...] | None,
    requested_state: str | None,
) -> ControlAction:
    """Persist intent before any external write. Never stores secrets."""
    action = ControlAction(
        command_id=command_id,
        source_run_id=source_run_id,
        interval_start=interval_start,
        intent_json=_safe_json(intent),
        authorization_allowed=authorization_allowed,
        blockers_json=_safe_json(list(blockers or [])),
        requested_state=requested_state,
        result="pending",
    )
    session.add(action)
    session.flush()
    return action


def finalize_action(
    session: Session,
    command_id: str,
    *,
    observed_state: str | None,
    physical: object | None,
    result: str,
    error_code: str | None = None,
    latency_ms: float | None = None,
) -> ControlAction | None:
    action = session.get(ControlAction, command_id)
    if action is None:
        return None
    action.observed_state = observed_state
    action.physical_json = _safe_json(physical)
    action.result = result
    action.error_code = error_code
    action.latency_ms = latency_ms
    action.updated_at = utcnow()
    return action


def list_pending_actions(session: Session) -> list[ControlAction]:
    return list(
        session.execute(select(ControlAction).where(ControlAction.result == "pending"))
        .scalars()
        .all()
    )


def try_acquire_lease(
    session: Session,
    *,
    owner_id: str,
    target_key: str = DEFAULT_SITE_KEY,
    ttl_seconds: float = 60.0,
    now: dt.datetime | None = None,
) -> bool:
    """Atomic compare-and-swap lease acquisition/renewal for one site key."""
    now = _aware(now or utcnow())
    expires = now + dt.timedelta(seconds=ttl_seconds)
    lease = session.get(ControllerLease, target_key)
    if lease is None:
        session.add(
            ControllerLease(
                target_key=target_key,
                owner_id=owner_id,
                acquired_at=now,
                renewed_at=now,
                expires_at=expires,
            )
        )
        session.flush()
        return True
    lease_expires = _aware(lease.expires_at)
    if lease.owner_id == owner_id or lease_expires <= now:
        lease.owner_id = owner_id
        if lease_expires <= now:
            lease.acquired_at = now
        lease.renewed_at = now
        lease.expires_at = expires
        session.flush()
        return True
    return False


def release_lease(
    session: Session,
    *,
    owner_id: str,
    target_key: str = DEFAULT_SITE_KEY,
) -> bool:
    lease = session.get(ControllerLease, target_key)
    if lease is None:
        return True
    if lease.owner_id != owner_id:
        return False
    session.delete(lease)
    session.flush()
    return True


def lease_held_by(
    session: Session,
    *,
    owner_id: str,
    target_key: str = DEFAULT_SITE_KEY,
    now: dt.datetime | None = None,
) -> bool:
    now = _aware(now or utcnow())
    lease = session.get(ControllerLease, target_key)
    return bool(lease and lease.owner_id == owner_id and _aware(lease.expires_at) > now)


def set_lockout(
    session: Session,
    *,
    reason: str,
    duration_seconds: float,
    now: dt.datetime | None = None,
) -> ControllerStateRow:
    now = _aware(now or utcnow())
    row = ensure_controller_state(session)
    row.state = "LOCKOUT"
    row.lockout_reason = reason
    row.lockout_until = now + dt.timedelta(seconds=duration_seconds)
    row.updated_at = now
    return row


def clear_lockout(session: Session, *, now: dt.datetime | None = None) -> ControllerStateRow:
    now = _aware(now or utcnow())
    row = ensure_controller_state(session)
    row.state = "DISARMED"
    row.lockout_reason = None
    row.lockout_until = None
    row.consecutive_failures = 0
    row.updated_at = now
    return row


def is_locked_out(session: Session, *, now: dt.datetime | None = None) -> bool:
    now = _aware(now or utcnow())
    row = ensure_controller_state(session)
    return bool(row.lockout_until is not None and _aware(row.lockout_until) > now)


def new_owner_id() -> str:
    return str(uuid.uuid4())


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _safe_json(value: object | None) -> str | None:
    if value is None:
        return None
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    text = json.dumps(value, default=str, sort_keys=True)
    lowered = text.lower()
    for needle in ("bearer ", "ha_token", "authorization", "password", "arm_token"):
        if needle in lowered:
            raise ValueError("refusing to persist secret-like control payload")
    return text
