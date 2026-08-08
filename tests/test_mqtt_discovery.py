from __future__ import annotations

from energy_optimizer.mqtt_publish import (
    BatteryControlMqttState,
    MqttConfig,
    MqttPublisher,
    RecommendationState,
)


def test_discovery_configs_shape() -> None:
    pub = MqttPublisher(MqttConfig(host="localhost", node_id="energy_optimizer"))
    configs = pub.build_discovery_configs()
    # Recommendation sensors/binary + battery control sensors/binary.
    assert len(configs) == 16
    next_action_topic = "homeassistant/sensor/energy_optimizer/next_action/config"
    assert next_action_topic in configs
    cfg = configs[next_action_topic]
    assert cfg["state_topic"] == "energy_optimizer/state"
    assert cfg["availability_topic"] == "energy_optimizer/status"
    assert cfg["unique_id"] == "energy_optimizer_next_action"

    control_topic = "homeassistant/binary_sensor/energy_optimizer/control_enabled/config"
    assert control_topic in configs
    assert configs[control_topic]["payload_on"] == "ON"

    battery_state = "homeassistant/sensor/energy_optimizer/battery_control_state/config"
    assert battery_state in configs
    assert configs[battery_state]["state_topic"] == "energy_optimizer/battery_control/state"

    armed = "homeassistant/binary_sensor/energy_optimizer/battery_control_armed/config"
    assert armed in configs
    assert configs[armed]["state_topic"] == "energy_optimizer/battery_control/state"


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


def test_battery_control_mqtt_payload_never_claims_watchdog_by_default() -> None:
    state = BatteryControlMqttState(
        battery_control_state="DISARMED",
        battery_control_effective="DRY_RUN",
        battery_control_last_result="dry_run_skipped",
        battery_control_blockers="dry_run_or_disarmed",
        battery_control_armed=False,
        battery_control_lease_held=False,
    )
    payload = state.as_payload()
    assert payload["battery_control_watchdog_healthy"] == "OFF"
    assert payload["battery_control_armed"] == "OFF"
