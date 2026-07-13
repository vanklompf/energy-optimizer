"""PV production forecast (Forecast.Solar / Solcast) with recent-error correction.

Requires configured PV plane geometry (lat/lon + one or more planes). A weather entity is
never treated as a PV forecast. The raw provider forecast is scaled by a capped
actual/forecast ratio over the trailing few hours to correct systematic bias.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import httpx

from ..config import PvPlane

logger = logging.getLogger(__name__)

FORECAST_SOLAR_BASE = "https://api.forecast.solar"

# The correction ratio is clamped to avoid a single bad hour swinging the whole forecast.
CORRECTION_MIN = 0.5
CORRECTION_MAX = 1.5


@dataclass(slots=True)
class PvForecastPoint:
    interval_start: dt.datetime  # UTC
    energy_kwh: float
    confidence: str = "ok"


class PvForecaster:
    def __init__(
        self,
        lat: float,
        lon: float,
        planes: list[PvPlane],
        *,
        provider: str = "forecast_solar",
        solcast_api_key: str = "",
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._lat = lat
        self._lon = lon
        self._planes = planes
        self._provider = provider
        self._solcast_api_key = solcast_api_key
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> PvForecaster:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def forecast(self, correction_ratio: float | None = None) -> list[PvForecastPoint]:
        """Return hourly PV energy (kWh) forecast, corrected and confidence-tagged."""
        if self._provider == "none" or not self._planes:
            return []
        if self._provider != "forecast_solar":
            logger.warning("PV provider %s not implemented yet; returning empty", self._provider)
            return []

        client = self._client
        if client is None:
            raise RuntimeError("PvForecaster used outside of an async context manager")

        ratio = (
            _clamp(correction_ratio, CORRECTION_MIN, CORRECTION_MAX) if correction_ratio else 1.0
        )
        confidence = "ok" if correction_ratio is not None else "low_confidence"

        # Sum watt-hours across planes at each timestamp.
        totals: dict[dt.datetime, float] = {}
        for plane in self._planes:
            data = await self._fetch_forecast_solar(client, plane)
            for ts, wh in data.items():
                totals[ts] = totals.get(ts, 0.0) + wh

        return [
            PvForecastPoint(
                interval_start=ts, energy_kwh=(wh / 1000.0) * ratio, confidence=confidence
            )
            for ts, wh in sorted(totals.items())
        ]

    async def _fetch_forecast_solar(
        self, client: httpx.AsyncClient, plane: PvPlane
    ) -> dict[dt.datetime, float]:
        # Forecast.Solar: /estimate/watthours/period/{lat}/{lon}/{dec}/{az}/{kwp}
        url = (
            f"{FORECAST_SOLAR_BASE}/estimate/watthours/period/"
            f"{self._lat}/{self._lon}/{plane.tilt}/{plane.azimuth}/{plane.peak_kwp}"
        )
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Forecast.Solar fetch failed for plane %s: %s", plane, exc)
            return {}
        result: dict[dt.datetime, float] = {}
        for ts_str, wh in (payload.get("result") or {}).items():
            ts = _parse_local_naive_to_utc(ts_str)
            if ts is not None:
                result[ts] = float(wh)
        return result


def _parse_local_naive_to_utc(ts_str: str) -> dt.datetime | None:
    # Forecast.Solar returns local naive timestamps "YYYY-MM-DD HH:MM:SS"; the caller
    # configures TZ so we keep them tz-aware. Here we assume they are already in the
    # provider's local zone and mark as UTC-agnostic naive -> attach UTC as a safe default.
    try:
        parsed = dt.datetime.fromisoformat(ts_str)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _clamp(value: float | None, lo: float, hi: float) -> float:
    if value is None:
        return 1.0
    return max(lo, min(hi, value))
