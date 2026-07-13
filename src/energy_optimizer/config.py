"""Application configuration via Pydantic Settings.

All configuration comes from environment variables (prefix ``EO_``) or an env file.
Nothing is read from anywhere else; this module is the single source of truth for
runtime configuration and derived constants (e.g. one-way efficiencies).
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    mode: Literal["dry_run"] = "dry_run"
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
    battery_soc_min_pct: float = 20.0
    battery_soc_max_pct: float = 98.0
    battery_round_trip_efficiency: float = 0.90
    degradation_cost_pln_per_kwh: float = 0.05

    site_import_limit_kw: float = 14.0
    site_export_limit_kw: float = 14.0
    inverter_limit_kw: float = 12.0

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

    # --- PV forecast ---
    pv_lat: float = 51.9194
    pv_lon: float = 19.1451
    pv_planes: list[PvPlane] = Field(
        default_factory=lambda: [PvPlane(peak_kwp=7.0, tilt=35, azimuth=0)]
    )
    pv_forecast_provider: Literal["forecast_solar", "solcast", "none"] = "forecast_solar"
    solcast_api_key: str = ""

    # --- InfluxDB bootstrap (optional, read-only) ---
    influxdb_bootstrap_enabled: bool = False
    influxdb_url: str = ""
    influxdb_token: str = ""
    influxdb_org: str = ""
    influxdb_bucket: str = "ha_raw"

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
    def soc_max_kwh(self) -> float:
        return self.battery_capacity_kwh * self.battery_soc_max_pct / 100.0

    @property
    def step_hours(self) -> float:
        return self.step_minutes / 60.0

    @field_validator("battery_round_trip_efficiency")
    @classmethod
    def _validate_efficiency(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("battery_round_trip_efficiency must be in (0, 1]")
        return v

    @field_validator("pv_planes", mode="before")
    @classmethod
    def _parse_pv_planes(cls, v: object) -> object:
        # pydantic-settings will JSON-decode complex env values automatically, but be
        # defensive if a raw string sneaks through.
        if isinstance(v, str):
            import json

            return json.loads(v)
        return v


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance for use as a FastAPI dependency and elsewhere."""
    return Settings()
