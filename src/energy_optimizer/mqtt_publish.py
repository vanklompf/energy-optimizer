"""MQTT discovery config + state publishing with LWT availability.

Publishes HA MQTT-discovery configs for the recommendation sensors, then publishes state
on demand. Availability is tracked via a Last-Will on ``<node>/status`` so HA marks the
sensors unavailable if the app dies. Discovery and availability are retained; recommendation
state is retained. Battery heartbeat is never retained so a stale "active" cannot outlive
the process.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MqttConfig:
    host: str
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    discovery_prefix: str = "homeassistant"
    node_id: str = "energy_optimizer"
    client_id: str = "energy_optimizer"


# (object_id, component, name, device_class/None, unit/None)
RECOMMENDATION_SENSORS: list[tuple[str, str, str, str | None, str | None]] = [
    ("next_action", "sensor", "Next action", None, None),
    ("next_action_power_kw", "sensor", "Next action power", "power", "kW"),
    ("target_soc", "sensor", "Target SoC", "battery", "%"),
    ("expected_profit_today", "sensor", "Expected profit today", "monetary", "PLN"),
    ("actual_cost_today", "sensor", "Actual cost today", "monetary", "PLN"),
    ("missed_opportunity_today", "sensor", "Missed opportunity today", "monetary", "PLN"),
    ("decision_reason", "sensor", "Decision reason", None, None),
    ("confidence", "sensor", "Confidence", None, None),
]

BATTERY_SENSORS: list[tuple[str, str, str, str | None, str | None]] = [
    ("battery_control_state", "sensor", "Battery control state", None, None),
    ("battery_control_effective", "sensor", "Battery control effective", None, None),
    ("battery_control_last_result", "sensor", "Battery control last result", None, None),
    ("battery_control_blockers", "sensor", "Battery control blockers", None, None),
]

# Backwards-compatible alias used by older imports/tests.
SENSORS = RECOMMENDATION_SENSORS + BATTERY_SENSORS

RECOMMENDATION_BINARY_SENSORS: list[tuple[str, str]] = [
    ("control_enabled", "Control enabled"),
]

BATTERY_BINARY_SENSORS: list[tuple[str, str]] = [
    ("battery_control_armed", "Battery control armed"),
    ("battery_control_lease_held", "Battery control lease held"),
    ("battery_control_watchdog_healthy", "Battery control watchdog healthy"),
]

BINARY_SENSORS = RECOMMENDATION_BINARY_SENSORS + BATTERY_BINARY_SENSORS


@dataclass(slots=True)
class RecommendationState:
    next_action: str
    next_action_power_kw: float
    target_soc: float
    expected_profit_today: float
    actual_cost_today: float
    missed_opportunity_today: float
    decision_reason: str
    confidence: str
    control_enabled: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "next_action": self.next_action,
            "next_action_power_kw": round(self.next_action_power_kw, 3),
            "target_soc": round(self.target_soc, 1),
            "expected_profit_today": round(self.expected_profit_today, 2),
            "actual_cost_today": round(self.actual_cost_today, 2),
            "missed_opportunity_today": round(self.missed_opportunity_today, 2),
            "decision_reason": self.decision_reason,
            "confidence": self.confidence,
            "control_enabled": "ON" if self.control_enabled else "OFF",
        }


@dataclass(slots=True)
class BatteryControlMqttState:
    battery_control_state: str
    battery_control_effective: str
    battery_control_last_result: str
    battery_control_blockers: str
    battery_control_armed: bool
    battery_control_lease_held: bool
    battery_control_watchdog_healthy: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "battery_control_state": self.battery_control_state,
            "battery_control_effective": self.battery_control_effective,
            "battery_control_last_result": self.battery_control_last_result,
            "battery_control_blockers": self.battery_control_blockers,
            "battery_control_armed": "ON" if self.battery_control_armed else "OFF",
            "battery_control_lease_held": "ON" if self.battery_control_lease_held else "OFF",
            "battery_control_watchdog_healthy": (
                "ON" if self.battery_control_watchdog_healthy else "OFF"
            ),
        }


class MqttPublisher:
    def __init__(self, config: MqttConfig) -> None:
        self._cfg = config
        self._availability_topic = f"{config.node_id}/status"
        self._state_topic = f"{config.node_id}/state"
        self._battery_state_topic = f"{config.node_id}/battery_control/state"
        self._battery_heartbeat_topic = f"{config.node_id}/battery_control/heartbeat"
        self._client: mqtt.Client | None = None

    @property
    def device_info(self) -> dict[str, object]:
        return {
            "identifiers": [self._cfg.node_id],
            "name": "Energy Optimizer",
            "manufacturer": "energy-optimizer",
            "model": "dry-run recommender",
        }

    def connect(self) -> None:
        client = mqtt.Client(  # type: ignore[misc]
            mqtt.CallbackAPIVersion.VERSION2,  # type: ignore[attr-defined]
            client_id=self._cfg.client_id,
        )
        if self._cfg.username:
            client.username_pw_set(self._cfg.username, self._cfg.password)
        if self._cfg.tls:
            client.tls_set()
        client.will_set(self._availability_topic, payload="offline", qos=1, retain=True)
        client.connect(self._cfg.host, self._cfg.port, keepalive=60)
        client.loop_start()
        self._client = client
        client.publish(self._availability_topic, "online", qos=1, retain=True)
        logger.info("MQTT connected to %s:%s", self._cfg.host, self._cfg.port)

    def disconnect(self) -> None:
        if self._client is not None:
            self._client.publish(self._availability_topic, "offline", qos=1, retain=True)
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None

    def publish_discovery(self) -> None:
        """Publish retained discovery configs for all sensors."""
        client = self._require_client()
        for topic, config in self.build_discovery_configs().items():
            client.publish(topic, json.dumps(config), qos=1, retain=True)
        logger.info(
            "Published MQTT discovery for %d entities",
            len(SENSORS) + len(BINARY_SENSORS),
        )

    def publish_state(self, state: RecommendationState) -> None:
        client = self._require_client()
        client.publish(self._state_topic, json.dumps(state.as_payload()), qos=1, retain=True)

    def publish_battery_control_state(self, state: BatteryControlMqttState) -> None:
        """Retained operator status only — not a liveness claim (use heartbeat for that)."""
        client = self._require_client()
        client.publish(
            self._battery_state_topic,
            json.dumps(state.as_payload()),
            qos=1,
            retain=True,
        )

    def publish_battery_heartbeat(self, payload: dict[str, object]) -> None:
        """Publish a non-retained heartbeat so stale 'active' cannot outlive the process."""
        client = self._require_client()
        client.publish(
            self._battery_heartbeat_topic,
            json.dumps(payload),
            qos=1,
            retain=False,
        )

    def build_discovery_configs(self) -> dict[str, dict[str, object]]:
        """Return discovery topic -> config, without publishing (useful for tests)."""
        configs: dict[str, dict[str, object]] = {}
        for object_id, component, name, device_class, unit in RECOMMENDATION_SENSORS:
            cfg = self._base_config(object_id, name, state_topic=self._state_topic)
            cfg["value_template"] = f"{{{{ value_json.{object_id} }}}}"
            if device_class:
                cfg["device_class"] = device_class
            if unit:
                cfg["unit_of_measurement"] = unit
            configs[self._discovery_topic(component, object_id)] = cfg
        for object_id, name in RECOMMENDATION_BINARY_SENSORS:
            cfg = self._base_config(object_id, name, state_topic=self._state_topic)
            cfg["value_template"] = f"{{{{ value_json.{object_id} }}}}"
            cfg["payload_on"] = "ON"
            cfg["payload_off"] = "OFF"
            configs[self._discovery_topic("binary_sensor", object_id)] = cfg
        for object_id, component, name, device_class, unit in BATTERY_SENSORS:
            cfg = self._base_config(object_id, name, state_topic=self._battery_state_topic)
            cfg["value_template"] = f"{{{{ value_json.{object_id} }}}}"
            if device_class:
                cfg["device_class"] = device_class
            if unit:
                cfg["unit_of_measurement"] = unit
            configs[self._discovery_topic(component, object_id)] = cfg
        for object_id, name in BATTERY_BINARY_SENSORS:
            cfg = self._base_config(object_id, name, state_topic=self._battery_state_topic)
            cfg["value_template"] = f"{{{{ value_json.{object_id} }}}}"
            cfg["payload_on"] = "ON"
            cfg["payload_off"] = "OFF"
            configs[self._discovery_topic("binary_sensor", object_id)] = cfg
        return configs

    def _discovery_topic(self, component: str, object_id: str) -> str:
        return f"{self._cfg.discovery_prefix}/{component}/{self._cfg.node_id}/{object_id}/config"

    def _base_config(
        self, object_id: str, name: str, *, state_topic: str | None = None
    ) -> dict[str, object]:
        return {
            "name": name,
            "unique_id": f"{self._cfg.node_id}_{object_id}",
            "object_id": f"{self._cfg.node_id}_{object_id}",
            "state_topic": state_topic or self._state_topic,
            "availability_topic": self._availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": self.device_info,
        }

    def _require_client(self) -> mqtt.Client:
        if self._client is None:
            raise RuntimeError("MqttPublisher not connected")
        return self._client
