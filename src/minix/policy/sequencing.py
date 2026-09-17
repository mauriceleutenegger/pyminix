"""DAC and enable sequencing (§9.2).

Each plan_* function returns a list of steps for the controller to
execute: plain data, no I/O. The plans follow the reference, with the
project's decisions where they differ:

* Setpoint changes use the reference's per-family ordering. On NSI units
  the current goes first only if the new voltage with the old current
  would reach the power limit, and each write waits for its monitor to
  settle. On non-NSI units the current goes first when the voltage rises,
  with fixed 1 s delays. Both orders keep every intermediate state under
  the limit; plan_commit() checks this and refuses a plan that does not.
  The non-NSI one-step DAC correction is left out: it could push the
  current above the committed, power-checked value.
* Energizing keeps the vendor order (enables first, then ramp from zero),
  but writes zero to both DACs first instead of assuming they are there.
  MONX is required after the ramp, because it is not known whether MONX
  asserts with zero setpoints.
* A settle wait that runs out is reported, not treated as a fault; like
  the reference, the plan carries on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .. import protocol as p
from ..config import UnitConfig
from .limits import DacSetpoints
from .ranging import OUT_OF_RANGE_TOLERANCE

# Reference timing (§9.2).
NSI_PAUSE_S = 0.25
NSI_FINAL_PAUSE_S = 0.5
NSI_SETTLE_INTERVAL_S = 0.5
NSI_SETTLE_ATTEMPTS = 15
NON_NSI_DELAY_S = 1.0
NSI_HV_OFF_PAUSES_S = (0.2, 0.1, 0.5)
NSI_STARTUP_PAUSE_S = 0.2


@dataclass(frozen=True)
class WriteDac:
    channel: int            # protocol.DAC_HV or protocol.DAC_CURRENT
    counts: int


@dataclass(frozen=True)
class SetEnable:
    on: bool                # the device checks the interlock and reads back


@dataclass(frozen=True)
class Wait:
    seconds: float


@dataclass(frozen=True)
class WaitForSettle:
    """Poll a monitor until it is in range, or give up and report."""

    channel: int            # protocol.ADC_HV or protocol.ADC_CURRENT
    expected: float         # kV or µA
    tolerance: float = OUT_OF_RANGE_TOLERANCE
    interval_s: float = NSI_SETTLE_INTERVAL_S
    attempts: int = NSI_SETTLE_ATTEMPTS


@dataclass(frozen=True)
class RequireMonx:
    """Fail unless MONX (tube ready) asserts within the timeout."""

    timeout_s: float


@dataclass(frozen=True)
class ConfigureTemperature:
    pass


Step = Union[WriteDac, SetEnable, Wait, WaitForSettle, RequireMonx, ConfigureTemperature]


class PlanError(ValueError):
    """A plan would pass through a state above the power limit."""


def plan_commit(previous: DacSetpoints, target: DacSetpoints, unit: UnitConfig) -> list[Step]:
    """Change the DACs from previous to target with HV enabled."""
    if unit.is_nsi:
        steps = _nsi_commit(previous, target, unit)
    else:
        steps = _non_nsi_commit(previous, target)
    peak = peak_power_mw(previous, steps)
    if peak >= unit.safe_mw:
        raise PlanError(f"plan from {previous} to {target} reaches {peak:.0f} mW, "
                        f"limit {unit.safe_mw:.0f} mW")
    return steps


def plan_energize(target: DacSetpoints, unit: UnitConfig, monx_timeout_s: float) -> list[Step]:
    """Switch HV on and ramp from zero to target."""
    zero = DacSetpoints.zero(unit)
    return [
        WriteDac(p.DAC_HV, 0),
        WriteDac(p.DAC_CURRENT, 0),
        SetEnable(True),
        *plan_commit(zero, target, unit),
        RequireMonx(monx_timeout_s),
    ]


def plan_deenergize(unit: UnitConfig) -> list[Step]:
    """Ramp both DACs to zero, then switch HV off (the reference order)."""
    hv_pause, current_pause, final_pause = NSI_HV_OFF_PAUSES_S if unit.is_nsi else (0, 0, 0)
    return _without_empty_waits([
        WriteDac(p.DAC_HV, 0),
        Wait(hv_pause),
        WriteDac(p.DAC_CURRENT, 0),
        Wait(current_pause),
        SetEnable(False),
        Wait(final_pause),
    ])


def plan_startup(unit: UnitConfig) -> list[Step]:
    """After initialize(): configure the temperature sensor and zero the DACs.

    The reference runs its whole startup twice, 2 s apart, for no stated
    reason (§5.2); the sync check in initialize() is relied on instead.
    """
    return _without_empty_waits([
        ConfigureTemperature(),
        WriteDac(p.DAC_HV, 0),
        Wait(NSI_STARTUP_PAUSE_S if unit.is_nsi else 0),
        WriteDac(p.DAC_CURRENT, 0),
    ])


def peak_power_mw(start: DacSetpoints, steps: list[Step]) -> float:
    """Highest setpoint power reached while applying the steps' DAC writes."""
    hv, current = start.hv_counts, start.current_counts
    peak = start.power_mw
    for step in steps:
        if isinstance(step, WriteDac):
            if step.channel == p.DAC_HV:
                hv = step.counts
            else:
                current = step.counts
            peak = max(peak, DacSetpoints(hv, current, start.hv_factor).power_mw)
    return peak


def _nsi_commit(previous: DacSetpoints, target: DacSetpoints, unit: UnitConfig) -> list[Step]:
    steps: list[Step] = []
    new_hv_old_current = DacSetpoints(target.hv_counts, previous.current_counts, target.hv_factor)
    current_first = (target.kv > previous.kv and new_hv_old_current.power_mw >= unit.safe_mw)
    if current_first:
        steps += [Wait(NSI_PAUSE_S), *_nsi_write_current(target)]
    steps += [WriteDac(p.DAC_HV, target.hv_counts), WaitForSettle(p.ADC_HV, target.kv)]
    if not current_first:
        # The reference writes the current here in both cases; after a
        # current-first write that repeats the same value, so it is skipped.
        steps += [Wait(NSI_PAUSE_S), *_nsi_write_current(target)]
    steps.append(Wait(NSI_FINAL_PAUSE_S))
    return steps


def _nsi_write_current(target: DacSetpoints) -> list[Step]:
    return [WriteDac(p.DAC_CURRENT, target.current_counts),
            WaitForSettle(p.ADC_CURRENT, target.ua)]


def _non_nsi_commit(previous: DacSetpoints, target: DacSetpoints) -> list[Step]:
    hv = WriteDac(p.DAC_HV, target.hv_counts)
    current = WriteDac(p.DAC_CURRENT, target.current_counts)
    first, second = (current, hv) if target.kv > previous.kv else (hv, current)
    return [first, Wait(NON_NSI_DELAY_S), second, Wait(NON_NSI_DELAY_S)]


def _without_empty_waits(steps: list[Step]) -> list[Step]:
    return [s for s in steps if not (isinstance(s, Wait) and s.seconds <= 0)]
