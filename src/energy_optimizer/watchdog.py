"""PvOpti-side heartbeat health and a non-actuating fake HA watchdog actor.

The real Home Assistant automation lives in the site-config repository. This module
encodes the shared contract so PvOpti tests can prove expiry semantics and the exact
fallback sequence without contacting live HA or inventing deployment YAML here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .config import Settings
from .ha_client import HaState


@dataclass(frozen=True, slots=True)
class HeartbeatSample:
    """One observed PvOpti heartbeat.

    ``retained`` must be False for liveness. A retained MQTT "active"/"online" payload
    that outlives the process must never look healthy.
    """

    ts: dt.datetime
    state: str = "alive"
    retained: bool = False
    source: str = "mqtt"


@dataclass(frozen=True, slots=True)
class WatchdogDecision:
    healthy: bool
    reason: str
    should_fallback: bool


@dataclass(slots=True)
class FakeHaWatchdog:
    """In-process stand-in for the HA automation / emergency helper.

    Records the exact service calls the site watchdog must issue. Never enables Remote EMS.
    """

    settings: Settings
    service_calls: list[tuple[str, str, dict]] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)
    # Simulated HA number display defect: entities may show 0.0; watchdog must ignore them.
    displayed_number_values: dict[str, float] = field(default_factory=dict)

    def evaluate(
        self,
        *,
        heartbeat: HeartbeatSample | None,
        now: dt.datetime,
        controller_lockout: bool = False,
        emergency_off: bool = False,
        mqtt_available: bool = True,
        remote_ems_on: bool = False,
        startup: bool = False,
    ) -> WatchdogDecision:
        if emergency_off:
            return WatchdogDecision(False, "emergency_helper", True)
        if controller_lockout:
            return WatchdogDecision(False, "controller_lockout", True)
        if not mqtt_available:
            return WatchdogDecision(False, "mqtt_unavailable", True)
        if startup and remote_ems_on:
            return WatchdogDecision(False, "startup_remote_ems_on", True)
        if heartbeat is None:
            return WatchdogDecision(False, "heartbeat_missing", True)
        if heartbeat.retained:
            return WatchdogDecision(False, "retained_heartbeat_untrusted", True)
        age = (now - heartbeat.ts).total_seconds()
        expiry = self.settings.battery_control_heartbeat_expiry_seconds
        if age < 0 or age > expiry:
            return WatchdogDecision(False, "heartbeat_expired", True)
        return WatchdogDecision(True, "ok", False)

    def run_fallback(self, reason: str) -> list[tuple[str, str, dict]]:
        """Execute the empirical fallback using configured restore values only.

        Order: mode Standby/fallback → restore 8.8/9.6 kW and 100/0% cut-offs → Remote EMS off.
        Never reads HA displayed 0.0 as the restore source. Never turns Remote EMS on.
        """
        s = self.settings
        calls: list[tuple[str, str, dict]] = [
            (
                "select",
                "select_option",
                {
                    "entity_id": s.battery_control_mode_select_entity,
                    "option": s.battery_control_fallback_mode,
                },
            ),
            (
                "number",
                "set_value",
                {
                    "entity_id": s.battery_control_charge_limit_entity,
                    "value": s.battery_control_local_charge_limit_kw,
                },
            ),
            (
                "number",
                "set_value",
                {
                    "entity_id": s.battery_control_discharge_limit_entity,
                    "value": s.battery_control_local_discharge_limit_kw,
                },
            ),
            (
                "number",
                "set_value",
                {
                    "entity_id": s.battery_control_charge_cutoff_entity,
                    "value": s.battery_control_local_charge_cutoff_pct,
                },
            ),
            (
                "number",
                "set_value",
                {
                    "entity_id": s.battery_control_discharge_cutoff_entity,
                    "value": s.battery_control_local_discharge_cutoff_pct,
                },
            ),
            (
                "switch",
                "turn_off",
                {"entity_id": s.battery_control_remote_ems_switch_entity},
            ),
        ]
        # Prove we would not have used defective displayed zeros even if present.
        for entity_id, _value in (
            (s.battery_control_charge_limit_entity, s.battery_control_local_charge_limit_kw),
            (s.battery_control_discharge_limit_entity, s.battery_control_local_discharge_limit_kw),
        ):
            displayed = self.displayed_number_values.get(entity_id)
            if displayed == 0.0:
                # Still write the configured restore; never copy displayed 0.0.
                pass
        self.service_calls.extend(calls)
        self.notifications.append(f"battery_control_fallback:{reason}")
        return calls

    def maybe_act(
        self,
        *,
        heartbeat: HeartbeatSample | None,
        now: dt.datetime,
        controller_lockout: bool = False,
        emergency_off: bool = False,
        mqtt_available: bool = True,
        remote_ems_on: bool = False,
        startup: bool = False,
    ) -> WatchdogDecision:
        decision = self.evaluate(
            heartbeat=heartbeat,
            now=now,
            controller_lockout=controller_lockout,
            emergency_off=emergency_off,
            mqtt_available=mqtt_available,
            remote_ems_on=remote_ems_on,
            startup=startup,
        )
        if decision.should_fallback:
            self.run_fallback(decision.reason)
        return decision


def heartbeat_is_healthy(
    heartbeat: HeartbeatSample | None,
    *,
    now: dt.datetime,
    expiry_seconds: float,
) -> bool:
    if heartbeat is None or heartbeat.retained:
        return False
    age = (now - heartbeat.ts).total_seconds()
    return 0 <= age <= expiry_seconds


@dataclass(slots=True)
class HeartbeatSilenceTracker:
    """Persist last valid heartbeat independently of the current MQTT trigger payload.

    A periodic/timer evaluation can call ``evaluate`` with ``incoming=None`` to prove
    silence detection when MQTT becomes completely quiet.
    """

    last_valid_ts: dt.datetime | None = None
    last_reject_reason: str | None = None

    def ingest(self, sample: HeartbeatSample | None, *, now: dt.datetime) -> bool:
        """Accept a fresh non-retained heartbeat. Reject malformed/future/retained."""
        if sample is None:
            self.last_reject_reason = "heartbeat_missing"
            return False
        if sample.retained:
            self.last_reject_reason = "retained_heartbeat_untrusted"
            return False
        if sample.ts > now:
            self.last_reject_reason = "heartbeat_from_future"
            return False
        if not sample.state:
            self.last_reject_reason = "heartbeat_malformed"
            return False
        self.last_valid_ts = sample.ts
        self.last_reject_reason = None
        return True

    def as_sample(self) -> HeartbeatSample | None:
        if self.last_valid_ts is None:
            return None
        return HeartbeatSample(ts=self.last_valid_ts, retained=False, source="persisted")

    def evaluate_silence(
        self,
        *,
        now: dt.datetime,
        expiry_seconds: float,
        incoming: HeartbeatSample | None = None,
    ) -> WatchdogDecision:
        """Evaluate health on a timer tick; optional ``incoming`` updates the tracker first."""
        if incoming is not None:
            self.ingest(incoming, now=now)
        sample = self.as_sample()
        if sample is None:
            return WatchdogDecision(False, "heartbeat_missing", True)
        age = (now - sample.ts).total_seconds()
        if age < 0 or age > expiry_seconds:
            return WatchdogDecision(False, "heartbeat_expired", True)
        return WatchdogDecision(True, "ok", False)


def watchdog_health_from_ha(
    readiness: HaState | None,
    acknowledgement: HaState | None,
    *,
    now: dt.datetime,
    timezone: str,
    expiry_seconds: float,
) -> tuple[bool, str]:
    """Validate HA-side watchdog readiness plus its observed heartbeat acknowledgement.

    The acknowledgement is an HA ``input_datetime`` intentionally written by the
    independent MQTT-ingestion automation. It is therefore evidence that HA, not
    just PvOpti's local database, is receiving current heartbeats.
    """
    if readiness is None or readiness.state != "on":
        return False, "watchdog_not_ready"
    if acknowledgement is None or acknowledgement.state in {"unknown", "unavailable", "none", ""}:
        return False, "watchdog_ack_missing"
    try:
        acknowledged_at = dt.datetime.strptime(
            acknowledgement.state, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=ZoneInfo(timezone))
    except (TypeError, ValueError):
        return False, "watchdog_ack_invalid"
    age = (now - acknowledged_at.astimezone(dt.UTC)).total_seconds()
    if age < 0 or age > expiry_seconds:
        return False, "watchdog_ack_stale"
    return True, "ok"
