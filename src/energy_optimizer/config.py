"""Application configuration via Pydantic Settings.

All configuration comes from environment variables (prefix ``EO_``) or an env file.
Nothing is read from anywhere else; this module is the single source of truth for
runtime configuration and derived constants (e.g. one-way efficiencies).
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Exact Remote EMS option strings from docs/sigenergy-control-contract.md.
SIGENERGY_MODE_PCS_REMOTE_CONTROL = "PCS Remote Control"
SIGENERGY_MODE_STANDBY = "Standby"
SIGENERGY_MODE_MAXIMUM_SELF_CONSUMPTION = "Maximum Self Consumption"
SIGENERGY_MODE_COMMAND_CHARGING_GRID_FIRST = "Command Charging (Grid First)"
SIGENERGY_MODE_COMMAND_CHARGING_PV_FIRST = "Command Charging (PV First)"
SIGENERGY_MODE_COMMAND_DISCHARGING_PV_FIRST = "Command Discharging (PV First)"
SIGENERGY_MODE_COMMAND_DISCHARGING_ESS_FIRST = "Command Discharging (ESS First)"
SIGENERGY_MODE_V2G = "V2G"
SIGENERGY_MODE_UNKNOWN = "Unknown"

SIGENERGY_KNOWN_MODES: frozenset[str] = frozenset(
    {
        SIGENERGY_MODE_PCS_REMOTE_CONTROL,
        SIGENERGY_MODE_STANDBY,
        SIGENERGY_MODE_MAXIMUM_SELF_CONSUMPTION,
        SIGENERGY_MODE_COMMAND_CHARGING_GRID_FIRST,
        SIGENERGY_MODE_COMMAND_CHARGING_PV_FIRST,
        SIGENERGY_MODE_COMMAND_DISCHARGING_PV_FIRST,
        SIGENERGY_MODE_COMMAND_DISCHARGING_ESS_FIRST,
        SIGENERGY_MODE_V2G,
        SIGENERGY_MODE_UNKNOWN,
    }
)

# Modes PvOpti may select. Unknown / PCS / V2G remain forbidden.
SIGENERGY_SELECTABLE_MODES: frozenset[str] = frozenset(
    {
        SIGENERGY_MODE_STANDBY,
        SIGENERGY_MODE_MAXIMUM_SELF_CONSUMPTION,
        SIGENERGY_MODE_COMMAND_CHARGING_GRID_FIRST,
        SIGENERGY_MODE_COMMAND_CHARGING_PV_FIRST,
        SIGENERGY_MODE_COMMAND_DISCHARGING_PV_FIRST,
        SIGENERGY_MODE_COMMAND_DISCHARGING_ESS_FIRST,
    }
)

BATTERY_CONTROL_DIRECTIONS: frozenset[str] = frozenset({"FALLBACK", "IDLE", "CHARGE", "DISCHARGE"})


class PvPlane(BaseModel):
    """A single PV array plane used by the PV forecaster."""

    peak_kwp: float
    tilt: float = Field(ge=0, le=90, description="declination from horizontal, degrees")
    azimuth: float = Field(
        default=0.0,
        description="degrees from south; -90=east, 0=south, 90=west (Forecast.Solar convention)",
    )
    inverter_limit_kw: float | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General ---
    mode: Literal["dry_run", "control"] = "dry_run"
    tz: str = "Europe/Warsaw"
    db: str = "/data/energy_optimizer.sqlite"
    http_host: str = "0.0.0.0"
    http_port: int = 8320
    log_level: str = "INFO"

    # --- Home Assistant ---
    ha_url: str = "http://homeassistant.local:8123"
    ha_token: str = ""
    ha_verify_ssl: bool = True

    # --- Pstryk ---
    pstryk_api_key: str = ""
    pstryk_base_url: str = "https://api.pstryk.pl"
    pstryk_history_bootstrap_days: int = 21

    # --- MQTT ---
    mqtt_enabled: bool = True
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False
    mqtt_discovery_prefix: str = "homeassistant"
    mqtt_node_id: str = "energy_optimizer"
    mqtt_client_id: str = "energy_optimizer"

    # --- Battery / site ---
    battery_capacity_kwh: float = 18.08
    battery_max_charge_kw: float = 8.8
    battery_max_discharge_kw: float = 9.6
    # Manufacturer/BMS protected usable-empty point. The ordinary operating reserve
    # is separate so EV charging can deliberately consume it without permitting
    # household discharge or economic export below the reserve.
    battery_hard_soc_min_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    battery_soc_min_pct: float = 15.0
    battery_soc_max_pct: float = 98.0
    battery_round_trip_efficiency: float = 0.90
    degradation_cost_pln_per_kwh: float = 0.05

    # Sigen Hybrid 6.0 TP2: 16 A grid breaker @ 400 V 3-phase, 6600 VA AC, 6.0 kW nominal
    site_import_limit_kw: float = 11.0
    site_export_limit_kw: float = 6.6
    inverter_limit_kw: float = 6.0

    # --- Pricing model ---
    import_price_adjustment_pln_kwh: float = 0.0

    # --- Optimiser feature flags / margins ---
    allow_battery_export: bool = True
    allow_grid_charging: bool = True
    minimum_export_spread_pln_kwh: float = 0.30
    grid_charge_margin_pln_kwh: float = 0.30
    terminal_soc_salvage_pln_kwh: float = 0.0
    optimise_horizon_hours: int = 48
    step_minutes: int = 15

    # --- EV / PHEV flexible load and Home Assistant relay control ---
    # Control stays opt-in. Override entity IDs in deployment config for the real site.
    ev_control_enabled: bool = False
    ev_switch_entity: str = "switch.ev_charger"
    ev_power_entity: str = "sensor.ev_charger_power"
    ev_soc_entity: str = "sensor.ev_state_of_charge"
    ev_charging_status_entity: str = "sensor.ev_charging_status"
    ev_charging_active_entity: str = "binary_sensor.ev_charging_active"
    ev_charge_to_100_entity: str = "input_boolean.ev_charge_to_100_once"
    ev_unplugged_status: str = "3"
    ev_start_charging_statuses: list[str] = Field(default_factory=lambda: ["2"])
    ev_active_charging_statuses: list[str] = Field(
        default_factory=lambda: ["0", "1", "2", "5", "6", "9", "10", "11", "12", "13", "14", "15"]
    )
    ev_fault_entities: list[str] = Field(
        default_factory=lambda: [
            "binary_sensor.ev_charger_overheating",
            "binary_sensor.ev_charger_overpowering",
            "binary_sensor.ev_charger_overcurrent",
            "binary_sensor.ev_charger_overvoltage",
        ]
    )
    ev_capacity_kwh: float = 10.9
    ev_charge_power_kw: float = 1.8
    ev_charge_efficiency: float = 0.90
    ev_minimum_target_soc_pct: float = 50.0
    ev_departure_hour: int = 9
    ev_min_on_minutes: int = 5
    ev_min_off_minutes: int = 5
    ev_power_start_grace_minutes: int = 5
    ev_min_charging_power_kw: float = 0.1
    # Shelly relay state can propagate slowly through weak garage Wi-Fi.
    ev_relay_settle_seconds: float = Field(default=5.0, ge=0, le=60)
    ev_relay_verify_interval_seconds: float = Field(default=2.0, gt=0, le=30)
    ev_relay_verify_timeout_seconds: float = Field(default=30.0, ge=1, le=120)
    ev_relay_failure_backoff_minutes: int = Field(default=30, ge=1, le=1440)
    # Only this conservative fraction of same-day forecast surplus backs optional EV energy.
    ev_forecast_surplus_factor: float = Field(default=0.8, ge=0, le=1)
    # Opportunistic EV charging requires the house battery to still be projected to
    # reach this SoC by the end of the current optimisation horizon.
    ev_battery_full_soc_pct: float = Field(default=95.0, ge=50.0, le=100.0)

    # --- Stationary battery control (fail-safe; independent of planner flags) ---
    # Live actuation requires mode=control AND battery_control_enabled.
    # EO_ALLOW_GRID_CHARGING / EO_ALLOW_BATTERY_EXPORT remain planner-only.
    battery_control_enabled: bool = False
    battery_export_enabled: bool = False
    battery_control_grid_charge_enabled: bool = False
    battery_control_authorize_discharge: bool = False
    battery_control_supported_directions: list[str] = Field(
        default_factory=lambda: ["FALLBACK", "IDLE", "CHARGE"]
    )

    battery_control_remote_ems_switch_entity: str = (
        "switch.sigen_plant_remote_ems_controlled_by_home_assistant"
    )
    battery_control_mode_select_entity: str = "select.sigen_plant_remote_ems_control_mode"
    battery_control_charge_limit_entity: str = "number.sigen_plant_ess_max_charging_limit"
    battery_control_discharge_limit_entity: str = "number.sigen_plant_ess_max_discharging_limit"
    battery_control_charge_cutoff_entity: str = (
        "number.sigen_plant_ess_charge_cut_off_state_of_charge"
    )
    battery_control_discharge_cutoff_entity: str = (
        "number.sigen_plant_ess_discharge_cut_off_state_of_charge"
    )
    battery_control_watchdog_health_entity: str = ""
    battery_control_watchdog_ack_entity: str = ""

    battery_control_mode_standby: str = SIGENERGY_MODE_STANDBY
    battery_control_mode_charge_grid_first: str = SIGENERGY_MODE_COMMAND_CHARGING_GRID_FIRST
    # Active command mode used when charging under Remote EMS.
    battery_control_command_mode: str = SIGENERGY_MODE_COMMAND_CHARGING_GRID_FIRST
    battery_control_fallback_mode: str = SIGENERGY_MODE_STANDBY

    # Explicit local-restore values; never populate from HA number-entity fallback states.
    battery_control_local_charge_limit_kw: float = 8.8
    battery_control_local_discharge_limit_kw: float = 9.6
    battery_control_local_charge_cutoff_pct: float = 100.0
    battery_control_local_discharge_cutoff_pct: float = 0.0

    battery_control_max_grid_import_kw: float = Field(default=11.0, ge=0)
    # Capped by the inverter AC limit by default (site export rating alone is not enough).
    battery_control_max_grid_export_kw: float = Field(default=6.0, ge=0)
    battery_control_max_charge_kw: float = Field(default=8.8, ge=0)
    battery_control_max_discharge_kw: float = Field(default=9.6, ge=0)
    battery_control_min_soc_pct: float = Field(default=15.0, ge=0, le=100)
    battery_control_max_soc_pct: float = Field(default=98.0, ge=0, le=100)
    battery_control_charge_cutoff_margin_pct: float = Field(default=0.5, ge=0, le=100)

    battery_control_cadence_seconds: float = Field(default=30.0, ge=0)
    battery_control_max_plan_age_seconds: float = Field(default=900.0, ge=0)
    battery_control_max_telemetry_age_seconds: float = Field(default=120.0, ge=0)
    battery_control_command_poll_seconds: float = Field(default=1.0, ge=0)
    battery_control_command_timeout_seconds: float = Field(default=30.0, ge=0)
    # Contract: physical Standby/command verification deadline is at least 15 seconds.
    battery_control_physical_verify_timeout_seconds: float = Field(default=15.0, ge=0)
    battery_control_heartbeat_interval_seconds: float = Field(default=15.0, ge=0)
    battery_control_heartbeat_expiry_seconds: float = Field(default=60.0, ge=0)
    battery_control_standby_neutral_band_kw: float = Field(default=0.12, ge=0)
    battery_control_deadband_kw: float = Field(default=0.12, ge=0)
    battery_control_max_power_step_kw: float = Field(default=0.5, ge=0)
    battery_control_retry_limit: int = Field(default=2, ge=0)
    battery_control_lockout_duration_seconds: float = Field(default=3600.0, ge=0)
    battery_control_activation_margin_pln_kwh: float = Field(default=0.05, ge=0)

    # --- PV forecast ---
    pv_lat: float = 51.9194
    pv_lon: float = 19.1451
    pv_planes: list[PvPlane] = Field(
        default_factory=lambda: [PvPlane(peak_kwp=7.0, tilt=35, azimuth=0)]
    )
    pv_forecast_provider: Literal["forecast_solar", "solcast", "none"] = "forecast_solar"
    solcast_api_key: str = ""

    # --- Derived ---
    @property
    def eta_charge(self) -> float:
        """One-way charge efficiency: sqrt(round-trip)."""
        return math.sqrt(self.battery_round_trip_efficiency)

    @property
    def eta_discharge(self) -> float:
        """One-way discharge efficiency: sqrt(round-trip)."""
        return math.sqrt(self.battery_round_trip_efficiency)

    @property
    def soc_min_kwh(self) -> float:
        return self.battery_capacity_kwh * self.battery_soc_min_pct / 100.0

    @property
    def hard_soc_min_kwh(self) -> float:
        return self.battery_capacity_kwh * self.battery_hard_soc_min_pct / 100.0

    @property
    def soc_max_kwh(self) -> float:
        return self.battery_capacity_kwh * self.battery_soc_max_pct / 100.0

    @property
    def step_hours(self) -> float:
        return self.step_minutes / 60.0

    @property
    def battery_actuation_live(self) -> bool:
        """True only when both live-control gates are on."""
        return self.mode == "control" and self.battery_control_enabled

    @field_validator("battery_round_trip_efficiency")
    @classmethod
    def _validate_efficiency(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("battery_round_trip_efficiency must be in (0, 1]")
        return v

    @model_validator(mode="after")
    def _validate_battery_soc_thresholds(self) -> Self:
        if not (
            self.battery_hard_soc_min_pct <= self.battery_soc_min_pct <= self.battery_soc_max_pct
        ):
            raise ValueError(
                "battery SoC thresholds must be ordered: hard floor <= reserve <= maximum"
            )
        return self

    @model_validator(mode="after")
    def _validate_battery_control_settings(self) -> Self:
        self._validate_battery_control_mode_strings()
        self._validate_battery_control_directions()
        self._validate_battery_control_limits_and_timings()
        if self.mode == "control" or self.battery_control_enabled:
            self._validate_battery_control_entities()
        if self.mode == "control":
            self._validate_battery_control_armed()
        return self

    def _validate_battery_control_mode_strings(self) -> None:
        mode_fields = {
            "battery_control_mode_standby": self.battery_control_mode_standby,
            "battery_control_mode_charge_grid_first": self.battery_control_mode_charge_grid_first,
            "battery_control_command_mode": self.battery_control_command_mode,
            "battery_control_fallback_mode": self.battery_control_fallback_mode,
        }
        for name, value in mode_fields.items():
            if value not in SIGENERGY_KNOWN_MODES:
                raise ValueError(f"{name} is not a known Sigenergy mode option: {value!r}")
            if value == SIGENERGY_MODE_UNKNOWN:
                raise ValueError(f"{name} must not use Unknown mode option")
            if value not in SIGENERGY_SELECTABLE_MODES:
                raise ValueError(f"{name} mode option is not selectable by PvOpti: {value!r}")
        if self.battery_control_command_mode == self.battery_control_fallback_mode:
            raise ValueError(
                "battery_control_command_mode must be distinct from battery_control_fallback_mode"
            )

    def _validate_battery_control_directions(self) -> None:
        directions = [d.strip().upper() for d in self.battery_control_supported_directions]
        if not directions:
            raise ValueError("battery_control_supported_directions must not be empty")
        unknown = [d for d in directions if d not in BATTERY_CONTROL_DIRECTIONS]
        if unknown:
            raise ValueError(f"unsupported control supported.direction value(s): {unknown}")
        object.__setattr__(self, "battery_control_supported_directions", directions)
        if self.battery_control_authorize_discharge and "DISCHARGE" not in directions:
            raise ValueError(
                "authorize_discharge requires DISCHARGE in battery_control_supported_directions"
            )

    def _validate_battery_control_entities(self) -> None:
        required = {
            "battery_control_remote_ems_switch_entity": (
                self.battery_control_remote_ems_switch_entity
            ),
            "battery_control_mode_select_entity": self.battery_control_mode_select_entity,
            "battery_control_charge_limit_entity": self.battery_control_charge_limit_entity,
            "battery_control_discharge_limit_entity": self.battery_control_discharge_limit_entity,
            "battery_control_charge_cutoff_entity": self.battery_control_charge_cutoff_entity,
            "battery_control_discharge_cutoff_entity": self.battery_control_discharge_cutoff_entity,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"battery control entity id(s) missing: {', '.join(missing)}")

    def _validate_battery_control_limits_and_timings(self) -> None:
        if self.battery_control_max_charge_kw <= 0:
            raise ValueError("battery_control_max_charge_kw must be positive")
        if self.battery_control_max_discharge_kw <= 0:
            raise ValueError("battery_control_max_discharge_kw must be positive")
        if self.battery_control_max_grid_import_kw <= 0:
            raise ValueError("battery_control_max_grid_import_kw must be positive")
        if self.battery_control_cadence_seconds <= 0:
            raise ValueError("battery_control_cadence_seconds must be positive")
        if self.battery_control_max_plan_age_seconds <= 0:
            raise ValueError("battery_control_max_plan_age_seconds must be positive")
        if self.battery_control_max_telemetry_age_seconds <= 0:
            raise ValueError("battery_control_max_telemetry_age_seconds must be positive")
        if self.battery_control_command_poll_seconds <= 0:
            raise ValueError("battery_control_command_poll_seconds must be positive")
        if self.battery_control_command_timeout_seconds <= 0:
            raise ValueError("battery_control_command_timeout_seconds must be positive")
        if self.battery_control_heartbeat_interval_seconds <= 0:
            raise ValueError("battery_control_heartbeat_interval_seconds must be positive")
        if self.battery_control_heartbeat_expiry_seconds <= 0:
            raise ValueError("battery_control_heartbeat_expiry_seconds must be positive")
        if self.battery_control_lockout_duration_seconds <= 0:
            raise ValueError("battery_control_lockout_duration_seconds must be positive")
        if self.battery_control_standby_neutral_band_kw <= 0:
            raise ValueError("battery_control_standby_neutral_band_kw must be positive")
        if self.battery_control_deadband_kw <= 0:
            raise ValueError("battery_control_deadband_kw must be positive")
        if self.battery_control_max_power_step_kw <= 0:
            raise ValueError("battery_control_max_power_step_kw must be positive")
        if self.battery_control_physical_verify_timeout_seconds < 15.0:
            raise ValueError(
                "battery_control_physical_verify_timeout_seconds must be at least 15 "
                "(Sigenergy physical verification contract)"
            )
        export_cap = min(self.site_export_limit_kw, self.inverter_limit_kw)
        if self.battery_control_max_grid_export_kw > export_cap + 1e-9:
            raise ValueError(
                "battery_control_max_grid_export_kw exceeds site/inverter export capability"
            )
        if self.battery_control_max_charge_kw > self.battery_max_charge_kw + 1e-9:
            raise ValueError("battery_control_max_charge_kw exceeds battery_max_charge_kw")
        if self.battery_control_max_discharge_kw > self.battery_max_discharge_kw + 1e-9:
            raise ValueError("battery_control_max_discharge_kw exceeds battery_max_discharge_kw")
        if not (
            self.battery_hard_soc_min_pct
            <= self.battery_control_min_soc_pct
            <= self.battery_control_max_soc_pct
            <= self.battery_soc_max_pct
        ):
            raise ValueError(
                "battery control SoC must be ordered: hard floor <= control minimum "
                "<= control maximum <= battery maximum"
            )
        if self.mode == "control" or self.battery_control_enabled:
            if self.battery_control_min_soc_pct < self.battery_soc_min_pct - 1e-9:
                raise ValueError(
                    "battery_control_min_soc_pct must be at least the operating reserve "
                    "(battery_soc_min_pct)"
                )

    def _validate_battery_control_armed(self) -> None:
        if not self.battery_control_enabled:
            raise ValueError("mode=control requires battery_control_enabled as an independent gate")

    @field_validator("pv_planes", mode="before")
    @classmethod
    def _parse_pv_planes(cls, v: object) -> object:
        # pydantic-settings will JSON-decode complex env values automatically, but be
        # defensive if a raw string sneaks through.
        if isinstance(v, str):
            import json

            return json.loads(v)
        return v

    @field_validator("battery_control_supported_directions", mode="before")
    @classmethod
    def _parse_supported_directions(cls, v: object) -> object:
        if isinstance(v, str):
            import json

            text = v.strip()
            if text.startswith("["):
                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance for use as a FastAPI dependency and elsewhere."""
    return Settings()
