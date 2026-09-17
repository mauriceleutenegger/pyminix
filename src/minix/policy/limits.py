"""Setpoint clamping and the power limit (§9.1).

commit_setpoints() turns a requested voltage and current into the DAC
counts that will be written, and reports every adjustment so the UI can
show it. It differs from the reference in three deliberate ways:

* The power check runs on the values that will actually be written, in DAC
  counts. The reference checked unrounded values and then wrote rounded
  ones, which could commit up to about 4.05 W on a 4 W unit (§9.1).
* Both values are truncated to the DAC grid, so a written value never
  exceeds the request, and a power reduction takes the largest current
  count that stays below the limit.
* The power check always runs. The reference skipped it when neither
  value had changed, which only matters for values it had already checked.

As in the reference, excess power is always taken from the current, never
the voltage, and the limit is recomputed from the rating on every call.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass

from .. import protocol as p
from ..config import UnitConfig


@dataclass(frozen=True)
class DacSetpoints:
    """Counts for the two DAC channels, with the unit's HV factor for conversion."""

    hv_counts: int
    current_counts: int
    hv_factor: float

    @classmethod
    def zero(cls, unit: UnitConfig) -> DacSetpoints:
        return cls(0, 0, unit.hv_factor)

    @property
    def kv(self) -> float:
        return p.counts_to_volts(self.hv_counts) * self.hv_factor

    @property
    def ua(self) -> float:
        return p.counts_to_volts(self.current_counts) * p.CURRENT_FACTOR

    @property
    def power_mw(self) -> float:
        return self.kv * self.ua


class AdjustmentKind(enum.Enum):
    HV_RAISED_TO_MIN = "voltage raised to the minimum"
    HV_LOWERED_TO_MAX = "voltage lowered to the maximum"
    CURRENT_RAISED_TO_MIN = "current raised to the minimum"
    CURRENT_LOWERED_TO_MAX = "current lowered to the maximum"
    CURRENT_REDUCED_FOR_POWER = "current reduced to stay under the power limit"


@dataclass(frozen=True)
class Adjustment:
    kind: AdjustmentKind
    requested: float
    committed: float

    @property
    def message(self) -> str:
        unit = "kV" if self.kind.name.startswith("HV") else "µA"
        return (f"{self.kind.value}: {_fmt(self.requested)} → "
                f"{_fmt(self.committed)} {unit}")


@dataclass(frozen=True)
class Commitment:
    setpoints: DacSetpoints
    requested_kv: float
    requested_ua: float
    adjustments: tuple[Adjustment, ...] = ()

    @property
    def kv(self) -> float:
        return self.setpoints.kv

    @property
    def ua(self) -> float:
        return self.setpoints.ua

    @property
    def power_mw(self) -> float:
        return self.setpoints.power_mw


def commit_setpoints(kv: float, ua: float, unit: UnitConfig) -> Commitment:
    """Clamp a request to the unit's ranges and power limit.

    Raises ValueError for a non-finite request.
    """
    if not (math.isfinite(kv) and math.isfinite(ua)):
        raise ValueError(f"setpoint must be a finite number: {kv} kV, {ua} µA")
    adjustments = []

    kv_clamped = min(max(kv, unit.hv_min_kv), unit.hv_max_kv)
    if kv < unit.hv_min_kv:
        adjustments.append(Adjustment(AdjustmentKind.HV_RAISED_TO_MIN, kv, kv_clamped))
    elif kv > unit.hv_max_kv:
        adjustments.append(Adjustment(AdjustmentKind.HV_LOWERED_TO_MAX, kv, kv_clamped))

    ua_clamped = min(max(ua, unit.current_min_ua), unit.current_max_ua)
    if ua < unit.current_min_ua:
        adjustments.append(Adjustment(AdjustmentKind.CURRENT_RAISED_TO_MIN, ua, ua_clamped))
    elif ua > unit.current_max_ua:
        adjustments.append(Adjustment(AdjustmentKind.CURRENT_LOWERED_TO_MAX, ua, ua_clamped))

    hv_counts = p.volts_to_counts(kv_clamped / unit.hv_factor)
    current_counts = p.volts_to_counts(ua_clamped / unit.current_factor)
    setpoints = DacSetpoints(hv_counts, current_counts, unit.hv_factor)

    safe_mw = unit.safe_mw
    if setpoints.power_mw >= safe_mw:
        current_counts = p.volts_to_counts(safe_mw / setpoints.kv / unit.current_factor)
        while DacSetpoints(hv_counts, current_counts, unit.hv_factor).power_mw >= safe_mw:
            current_counts -= 1
        reduced = DacSetpoints(hv_counts, current_counts, unit.hv_factor)
        if reduced.ua < unit.current_min_ua:
            raise ValueError(f"{unit.describe()}: no current in range is under the power limit "
                             f"at {reduced.kv} kV")
        adjustments.append(Adjustment(AdjustmentKind.CURRENT_REDUCED_FOR_POWER,
                                      ua_clamped, reduced.ua))
        setpoints = reduced

    return Commitment(setpoints, kv, ua, tuple(adjustments))


def _fmt(value: float) -> str:
    return f"{round(value, 4):g}"
