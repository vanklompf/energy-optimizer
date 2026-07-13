"""Pstryk unified-metrics client.

The 2026-04 API is a single unified-metrics endpoint. This client fetches hourly pricing
frames, parses buy/sell prices and their components, and reports the *known-price horizon*
(how far into the future real prices are available). Windows are UTC; conversion to local
time happens at presentation, never here.

Reference::

    GET {base}/integrations/meter-data/unified-metrics/
        ?metrics=pricing&resolution=hour
        &window_start=2026-07-12T00:00:00Z&window_end=2026-07-14T00:00:00Z
    Authorization: <api key, no Bearer prefix>

Response ``{ "frames": [...], "summary": {...} }`` with prices in
``frames[].metrics.pricing.{price_gross, full_price, price_prosumer_gross, is_cheap,
is_expensive, ...}``.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

UNIFIED_METRICS_PATH = "/integrations/meter-data/unified-metrics/"


@dataclass(slots=True)
class PriceFrame:
    """One hourly pricing frame parsed from a unified-metrics response."""

    interval_start: dt.datetime  # timezone-aware UTC
    buy_gross: float | None  # full_price if present else price_gross
    sell_gross: float | None  # price_prosumer_gross
    tge: float | None = None
    service: float | None = None
    distribution: float | None = None
    excise: float | None = None
    vat: float | None = None
    base: float | None = None
    price_gross: float | None = None
    full_price: float | None = None
    is_cheap: bool | None = None
    is_expensive: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PstrykError(RuntimeError):
    pass


class PstrykClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.pstryk.pl",
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> PstrykClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        # Pstryk expects the raw key in Authorization, no "Bearer" prefix.
        return {"Authorization": self._api_key, "Accept": "application/json"}

    async def fetch_pricing(
        self, window_start: dt.datetime, window_end: dt.datetime, resolution: str = "hour"
    ) -> list[PriceFrame]:
        """Fetch pricing frames for [window_start, window_end) in UTC."""
        client = self._client
        if client is None:
            raise PstrykError("PstrykClient used outside of an async context manager")

        params = {
            "metrics": "pricing",
            "resolution": resolution,
            "window_start": _to_utc_iso(window_start),
            "window_end": _to_utc_iso(window_end),
        }
        url = self._base_url + UNIFIED_METRICS_PATH

        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await client.get(url, params=params, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
                return _parse_frames(data)
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                logger.warning(
                    "Pstryk fetch failed (attempt %d/%d): %s", attempt, self._max_retries, exc
                )
        raise PstrykError(f"Pstryk pricing fetch failed after {self._max_retries} attempts") from (
            last_exc
        )


def known_price_horizon_hours(frames: list[PriceFrame], now: dt.datetime) -> float:
    """Hours of contiguous real prices available from ``now`` forward.

    Returns 0 if there is no frame covering the current hour. Frames are assumed hourly.
    """
    if not frames:
        return 0.0
    future = sorted(
        (f for f in frames if f.buy_gross is not None), key=lambda f: f.interval_start
    )
    if not future:
        return 0.0
    # Find the last contiguous hourly frame starting at or after the current hour.
    current_hour = now.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
    relevant = [f for f in future if f.interval_start >= current_hour]
    if not relevant or relevant[0].interval_start != current_hour:
        return 0.0
    horizon_end = relevant[0].interval_start
    for f in relevant:
        if f.interval_start == horizon_end:
            horizon_end = f.interval_start + dt.timedelta(hours=1)
        else:
            break
    return (horizon_end - current_hour).total_seconds() / 3600.0


def _parse_frames(data: dict[str, Any]) -> list[PriceFrame]:
    frames_raw = data.get("frames")
    if frames_raw is None:
        raise PstrykError("unified-metrics response missing 'frames'")

    out: list[PriceFrame] = []
    for fr in frames_raw:
        pricing = (fr.get("metrics") or {}).get("pricing") or {}
        start = _parse_ts(fr.get("start") or fr.get("window_start") or fr.get("timestamp"))
        if start is None:
            continue
        price_gross = _as_float(pricing.get("price_gross"))
        full_price = _as_float(pricing.get("full_price"))
        # Import price prefers full_price (already includes distribution/service/VAT/excise),
        # falling back to price_gross. Never re-add distribution on top.
        buy = full_price if full_price is not None else price_gross
        out.append(
            PriceFrame(
                interval_start=start,
                buy_gross=buy,
                sell_gross=_as_float(pricing.get("price_prosumer_gross")),
                tge=_as_float(pricing.get("price_net") or pricing.get("tge")),
                service=_as_float(pricing.get("service_price")),
                distribution=_as_float(
                    pricing.get("dist_price") or pricing.get("distribution_price")
                ),
                excise=_as_float(pricing.get("excise_price") or pricing.get("excise")),
                vat=_as_float(pricing.get("vat")),
                base=_as_float(pricing.get("base_price") or pricing.get("base")),
                price_gross=price_gross,
                full_price=full_price,
                is_cheap=_as_bool(pricing.get("is_cheap")),
                is_expensive=_as_bool(pricing.get("is_expensive")),
                raw=fr,
            )
        )
    out.sort(key=lambda f: f.interval_start)
    return out


def _to_utc_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(s)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)
