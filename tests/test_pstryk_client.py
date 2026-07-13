from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx

from energy_optimizer.pstryk_client import (
    PstrykClient,
    _parse_frames,
    known_price_horizon_hours,
)

SAMPLE = {
    "frames": [
        {
            "start": "2026-07-12T00:00:00Z",
            "metrics": {
                "pricing": {
                    "price_gross": 0.85,
                    "full_price": 1.05,
                    "price_prosumer_gross": 0.42,
                    "service_price": 0.10,
                    "dist_price": 0.20,
                    "is_cheap": True,
                    "is_expensive": False,
                }
            },
        },
        {
            "start": "2026-07-12T01:00:00Z",
            "metrics": {
                "pricing": {
                    "price_gross": 0.95,
                    "full_price": 1.15,
                    "price_prosumer_gross": 0.45,
                    "is_cheap": False,
                    "is_expensive": True,
                }
            },
        },
    ],
    "summary": {},
}


def test_parse_frames_prefers_full_price() -> None:
    frames = _parse_frames(SAMPLE)
    assert len(frames) == 2
    assert frames[0].buy_gross == 1.05  # full_price wins over price_gross
    assert frames[0].sell_gross == 0.42
    assert frames[0].distribution == 0.20
    assert frames[0].is_cheap is True


def test_parse_frames_falls_back_to_price_gross() -> None:
    data = {
        "frames": [
            {
                "start": "2026-07-12T00:00:00Z",
                "metrics": {"pricing": {"price_gross": 0.85, "price_prosumer_gross": 0.42}},
            }
        ]
    }
    frames = _parse_frames(data)
    assert frames[0].buy_gross == 0.85


def test_known_price_horizon() -> None:
    frames = _parse_frames(SAMPLE)
    now = dt.datetime(2026, 7, 12, 0, 30, tzinfo=dt.UTC)
    assert known_price_horizon_hours(frames, now) == 2.0


def test_known_price_horizon_gap_breaks() -> None:
    now = dt.datetime(2026, 7, 12, 0, 0, tzinfo=dt.UTC)
    data = {
        "frames": [
            {"start": "2026-07-12T00:00:00Z", "metrics": {"pricing": {"price_gross": 1.0}}},
            {"start": "2026-07-12T02:00:00Z", "metrics": {"pricing": {"price_gross": 1.0}}},
        ]
    }
    frames = _parse_frames(data)
    assert known_price_horizon_hours(frames, now) == 1.0


@respx.mock
async def test_fetch_pricing_sends_raw_auth_header() -> None:
    route = respx.get("https://api.pstryk.pl/integrations/meter-data/unified-metrics/").mock(
        return_value=httpx.Response(200, json=SAMPLE)
    )
    async with PstrykClient("secret-key") as client:
        frames = await client.fetch_pricing(
            dt.datetime(2026, 7, 12, tzinfo=dt.UTC),
            dt.datetime(2026, 7, 14, tzinfo=dt.UTC),
        )
    assert len(frames) == 2
    request = route.calls.last.request
    assert request.headers["Authorization"] == "secret-key"  # no Bearer prefix
    assert "Bearer" not in request.headers["Authorization"]


@respx.mock
async def test_fetch_pricing_retries_then_raises() -> None:
    respx.get("https://api.pstryk.pl/integrations/meter-data/unified-metrics/").mock(
        return_value=httpx.Response(500)
    )
    from energy_optimizer.pstryk_client import PstrykError

    async with PstrykClient("k", max_retries=2) as client:
        with pytest.raises(PstrykError):
            await client.fetch_pricing(
                dt.datetime(2026, 7, 12, tzinfo=dt.UTC),
                dt.datetime(2026, 7, 13, tzinfo=dt.UTC),
            )
