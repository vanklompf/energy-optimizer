"""Pydantic request/response schemas for the REST API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BacktestRequest(BaseModel):
    start: str = Field(description="ISO datetime (UTC) inclusive")
    end: str = Field(description="ISO datetime (UTC) exclusive")
    policies: list[str] = Field(
        default_factory=lambda: ["pv_only", "self_consumption"],
        description="baseline policies to compare against the optimiser and actual",
    )
    battery_overrides: dict[str, float] = Field(default_factory=dict)


class PolicyResult(BaseModel):
    policy: str
    net_cost_pln: float
    import_kwh: float
    export_kwh: float
    battery_throughput_kwh: float


class BacktestResponse(BaseModel):
    start: str
    end: str
    intervals: int
    results: list[PolicyResult]
