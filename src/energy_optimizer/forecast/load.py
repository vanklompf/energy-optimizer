"""Load forecast: rolling median by (hour-of-day, weekday/weekend) from stored telemetry.

Falls back gracefully and marks output low-confidence when there is not enough history.
The forecaster is pure given a set of historical samples so it is trivially testable; the
scheduler feeds it telemetry read from the store.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from zoneinfo import ZoneInfo

# Minimum distinct daily samples per (bucket) before we trust the median.
MIN_SAMPLES = 3
DEFAULT_LOOKBACK_DAYS = 28


@dataclass(slots=True)
class LoadSample:
    ts: dt.datetime  # UTC
    load_kw: float


@dataclass(slots=True)
class LoadForecastPoint:
    interval_start: dt.datetime  # UTC
    load_kwh: float
    confidence: str


class LoadForecaster:
    def __init__(
        self, tz: str = "Europe/Warsaw", lookback_days: int = DEFAULT_LOOKBACK_DAYS
    ) -> None:
        self._tz = ZoneInfo(tz)
        self._lookback_days = lookback_days

    def _bucket(self, ts_utc: dt.datetime) -> tuple[int, bool]:
        local = ts_utc.astimezone(self._tz)
        is_weekend = local.weekday() >= 5
        return local.hour, is_weekend

    def build_profile(self, samples: list[LoadSample]) -> dict[tuple[int, bool], float]:
        """Median load (kW) per (hour, weekend) bucket."""
        buckets: dict[tuple[int, bool], list[float]] = {}
        for s in samples:
            if s.load_kw is None:
                continue
            buckets.setdefault(self._bucket(s.ts), []).append(s.load_kw)
        return {k: statistics.median(v) for k, v in buckets.items() if v}

    def forecast(
        self,
        samples: list[LoadSample],
        intervals: list[tuple[dt.datetime, float]],
    ) -> list[LoadForecastPoint]:
        """Forecast load energy (kWh) for each (interval_start, dt_hours)."""
        profile = self.build_profile(samples)
        counts = self._bucket_counts(samples)
        overall = statistics.median([s.load_kw for s in samples]) if samples else 0.0

        out: list[LoadForecastPoint] = []
        for start, dt_hours in intervals:
            bucket = self._bucket(start)
            load_kw = profile.get(bucket)
            if load_kw is not None and counts.get(bucket, 0) >= MIN_SAMPLES:
                confidence = "ok"
            elif load_kw is not None:
                confidence = "low_confidence"
            else:
                load_kw = overall
                confidence = "low_confidence"
            out.append(
                LoadForecastPoint(
                    interval_start=start,
                    load_kwh=load_kw * dt_hours,
                    confidence=confidence,
                )
            )
        return out

    def _bucket_counts(self, samples: list[LoadSample]) -> dict[tuple[int, bool], int]:
        # Count distinct local dates contributing to each bucket.
        seen: dict[tuple[int, bool], set[dt.date]] = {}
        for s in samples:
            bucket = self._bucket(s.ts)
            local_date = s.ts.astimezone(self._tz).date()
            seen.setdefault(bucket, set()).add(local_date)
        return {k: len(v) for k, v in seen.items()}
