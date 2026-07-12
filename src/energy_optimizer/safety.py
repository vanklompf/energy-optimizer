"""Safety rules: produce blockers/warnings and own ``control_enabled``.

Enforced even in dry-run. ``control_enabled`` is hardcoded ``False`` in the MVP; the flag
and the rate-limit scaffolding exist so controlled mode can inherit them later. A run is
``blocked`` on any blocker, ``low_confidence`` on any warning, else ``ok``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Hardcoded off in the MVP. Do not make this configurable until controlled mode is designed.
CONTROL_ENABLED = False


class Status(StrEnum):
    OK = "ok"
    LOW_CONFIDENCE = "low_confidence"
    BLOCKED = "blocked"


@dataclass(slots=True)
class SafetyInputs:
    telemetry_stale: bool
    telemetry_stale_reasons: list[str]
    have_current_price: bool
    have_pv_forecast: bool
    have_load_forecast: bool
    known_price_hours: float
    horizon_hours: float


@dataclass(slots=True)
class SafetyReport:
    status: Status
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    control_enabled: bool = CONTROL_ENABLED

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "control_enabled": self.control_enabled,
        }


def evaluate(inputs: SafetyInputs) -> SafetyReport:
    blockers: list[str] = []
    warnings: list[str] = []

    if inputs.telemetry_stale:
        blockers.extend(inputs.telemetry_stale_reasons or ["telemetry stale"])
    if not inputs.have_current_price:
        blockers.append("missing Pstryk price for the current hour")

    if not inputs.have_pv_forecast:
        warnings.append("missing PV forecast; recommendation is low-confidence")
    if not inputs.have_load_forecast:
        warnings.append("missing load forecast; recommendation is low-confidence")
    if inputs.known_price_hours < inputs.horizon_hours:
        warnings.append(
            f"only {inputs.known_price_hours:.0f}h of real prices; "
            f"remaining {max(0.0, inputs.horizon_hours - inputs.known_price_hours):.0f}h are padded"
        )

    if blockers:
        status = Status.BLOCKED
    elif warnings:
        status = Status.LOW_CONFIDENCE
    else:
        status = Status.OK
    return SafetyReport(status=status, blockers=blockers, warnings=warnings)
