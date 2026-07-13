from __future__ import annotations

from energy_optimizer.mqtt_publish import MqttConfig, MqttPublisher, RecommendationState


def test_discovery_configs_shape() -> None:
    pub = MqttPublisher(MqttConfig(host="localhost", node_id="energy_optimizer"))
    configs = pub.build_discovery_configs()
    # One config per sensor + binary sensor.
    assert len(configs) == 9
    next_action_topic = (
        "homeassistant/sensor/energy_optimizer/next_action/config"
    )
    assert next_action_topic in configs
    cfg = configs[next_action_topic]
    assert cfg["state_topic"] == "energy_optimizer/state"
    assert cfg["availability_topic"] == "energy_optimizer/status"
    assert cfg["unique_id"] == "energy_optimizer_next_action"

    control_topic = "homeassistant/binary_sensor/energy_optimizer/control_enabled/config"
    assert control_topic in configs
    assert configs[control_topic]["payload_on"] == "ON"


def test_recommendation_payload() -> None:
    state = RecommendationState(
        next_action="charge",
        next_action_power_kw=3.14159,
        target_soc=55.55,
        expected_profit_today=1.234,
        actual_cost_today=0.0,
        missed_opportunity_today=0.0,
        decision_reason="test",
        confidence="ok",
        control_enabled=False,
    )
    payload = state.as_payload()
    assert payload["next_action"] == "charge"
    assert payload["next_action_power_kw"] == 3.142
    assert payload["control_enabled"] == "OFF"
