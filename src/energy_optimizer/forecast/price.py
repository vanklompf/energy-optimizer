"""Price padding beyond the known horizon (median by local hour, low-confidence).

Day-ahead prices publish tomorrow's hours in the early afternoon; before that the known
horizon can be ~10 h. Beyond real prices we pad with a median-by-local-hour estimate from
recent history, requiring a minimum sample count, else a conservative fallback. Padded
prices are always flagged low-confidence and buy/sell are padded separately.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from zoneinfo import ZoneInfo

MIN_SAMPLES = 5


@dataclass(slots=True)
class PriceSample:
    interval_start: dt.datetime  # UTC
    buy: float
    sell: float


@dataclass(slots=True)
class PaddedPrice:
    interval_start: dt.datetime  # UTC
    buy: float
    sell: float
    source: str  # "api" | "forecast"
    confidence: str  # "ok" | "low_confidence"


def _hour_medians(
    samples: list[PriceSample], tz: ZoneInfo
) -> tuple[dict[int, float], dict[int, float], dict[int, int]]:
    buy_by_hour: dict[int, list[float]] = {}
    sell_by_hour: dict[int, list[float]] = {}
    for s in samples:
        hour = s.interval_start.astimezone(tz).hour
        buy_by_hour.setdefault(hour, []).append(s.buy)
        sell_by_hour.setdefault(hour, []).append(s.sell)
    buy_med = {h: statistics.median(v) for h, v in buy_by_hour.items()}
    sell_med = {h: statistics.median(v) for h, v in sell_by_hour.items()}
    counts = {h: len(v) for h, v in buy_by_hour.items()}
    return buy_med, sell_med, counts


def pad_prices(
    known: list[PriceSample],
    history: list[PriceSample],
    target_intervals: list[dt.datetime],
    *,
    tz: str = "Europe/Warsaw",
    fallback_buy: float | None = None,
    fallback_sell: float | None = None,
) -> list[PaddedPrice]:
    """Return prices for each target interval, using real prices where known and padding
    the rest with hour-of-day medians (low-confidence) or a conservative fallback."""
    zone = ZoneInfo(tz)
    known_map = {k.interval_start: k for k in known}
    buy_med, sell_med, counts = _hour_medians(history, zone)

    global_buy = (
        fallback_buy if fallback_buy is not None else _median_or_zero([s.buy for s in history])
    )
    global_sell = (
        fallback_sell if fallback_sell is not None else _median_or_zero([s.sell for s in history])
    )

    out: list[PaddedPrice] = []
    for ts in target_intervals:
        real = known_map.get(ts)
        if real is not None:
            out.append(
                PaddedPrice(
                    interval_start=ts, buy=real.buy, sell=real.sell, source="api", confidence="ok"
                )
            )
            continue
        hour = ts.astimezone(zone).hour
        if counts.get(hour, 0) >= MIN_SAMPLES:
            buy = buy_med[hour]
            sell = sell_med[hour]
        else:
            buy = global_buy
            sell = global_sell
        out.append(
            PaddedPrice(
                interval_start=ts,
                buy=buy,
                sell=sell,
                source="forecast",
                confidence="low_confidence",
            )
        )
    return out


def _median_or_zero(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0
