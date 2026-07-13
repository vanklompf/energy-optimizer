"""MQTT discovery config + state publishing with LWT availability.

Publishes HA MQTT-discovery configs for the recommendation sensors, then publishes state
on demand. Availability is tracked via a Last-Will on ``<node>/status`` so HA marks the
sensors unavailable if the app dies. Discovery and availability are retained; state is
published retained so HA has a value after a reconnect.
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


# (object_id, component, name, device_class/None, unit/None, extra config)
SENSORS: list[tuple[str, str, str, str | None, str | None]] = [
    ("next_action", "sensor", "Next action", None, None),
    ("next_action_power_kw", "sensor", "Next action power", "power", "kW"),
    ("target_soc", "sensor", "Target SoC", "battery", "%"),
    ("expected_profit_today", "sensor", "Expected profit today", "monetary", "PLN"),
    ("actual_cost_today", "sensor", "Actual cost today", "monetary", "PLN"),
    ("missed_opportunity_today", "sensor", "Missed opportunity today", "monetary", "PLN"),
    ("decision_reason", "sensor", "Decision reason", None, None),
    ("confidence", "sensor", "Confidence", None, None),
]

BINARY_SENSORS: list[tuple[str, str]] = [
    ("control_enabled", "Control enabled"),
]


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


class MqttPublisher:
    def __init__(self, config: MqttConfig) -> None:
        self._cfg = config
        self._availability_topic = f"{config.node_id}/status"
        self._state_topic = f"{config.node_id}/state"
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
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
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
        for object_id, component, name, device_class, unit in SENSORS:
            topic = self._discovery_topic(component, object_id)
            config = self._base_config(object_id, name)
            config["value_template"] = f"{{{{ value_json.{object_id} }}}}"
            if device_class:
                config["device_class"] = device_class
            if unit:
                config["unit_of_measurement"] = unit
            client.publish(topic, json.dumps(config), qos=1, retain=True)

        for object_id, name in BINARY_SENSORS:
            topic = self._discovery_topic("binary_sensor", object_id)
            config = self._base_config(object_id, name)
            config["value_template"] = f"{{{{ value_json.{object_id} }}}}"
            config["payload_on"] = "ON"
            config["payload_off"] = "OFF"
            client.publish(topic, json.dumps(config), qos=1, retain=True)
        logger.info("Published MQTT discovery for %d entities", len(SENSORS) + len(BINARY_SENSORS))

    def publish_state(self, state: RecommendationState) -> None:
        client = self._require_client()
        client.publish(self._state_topic, json.dumps(state.as_payload()), qos=1, retain=True)

    def build_discovery_configs(self) -> dict[str, dict[str, object]]:
        """Return discovery topic -> config, without publishing (useful for tests)."""
        configs: dict[str, dict[str, object]] = {}
        for object_id, component, name, device_class, unit in SENSORS:
            cfg = self._base_config(object_id, name)
            cfg["value_template"] = f"{{{{ value_json.{object_id} }}}}"
            if device_class:
                cfg["device_class"] = device_class
            if unit:
                cfg["unit_of_measurement"] = unit
            configs[self._discovery_topic(component, object_id)] = cfg
        for object_id, name in BINARY_SENSORS:
            cfg = self._base_config(object_id, name)
            cfg["value_template"] = f"{{{{ value_json.{object_id} }}}}"
            cfg["payload_on"] = "ON"
            cfg["payload_off"] = "OFF"
            configs[self._discovery_topic("binary_sensor", object_id)] = cfg
        return configs

    def _discovery_topic(self, component: str, object_id: str) -> str:
        return f"{self._cfg.discovery_prefix}/{component}/{self._cfg.node_id}/{object_id}/config"

    def _base_config(self, object_id: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "unique_id": f"{self._cfg.node_id}_{object_id}",
            "object_id": f"{self._cfg.node_id}_{object_id}",
            "state_topic": self._state_topic,
            "availability_topic": self._availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": self.device_info,
        }

    def _require_client(self) -> mqtt.Client:
        if self._client is None:
            raise RuntimeError("MqttPublisher not connected")
        return self._client
