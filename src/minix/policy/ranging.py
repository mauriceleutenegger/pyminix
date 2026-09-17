"""Monitor range checking (§6.4).

The result drives an out-of-range indicator only. Like the reference, this
project does not drop HV on a range failure.

With HV on, both monitors are compared with the committed setpoints. With
HV off, nothing is tested until hv_off_test_delay_s has passed since HV was
last switched on or off; after that both monitors must read near zero.
The power check applies whenever testing is active. Power is computed from
the unrounded monitor values; the reference used readings rounded to 1 kV
and 1 µA, so the two can disagree by a few tens of mW.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import SafetyConfig, UnitConfig
from .limits import DacSetpoints

OUT_OF_RANGE_TOLERANCE = 0.10
ABSOLUTE_MARGIN = 1.0        # kV or µA, added to both sides of every band
HV_OFF_EXPECTED = 1.1        # the reference's expected reading with HV off


def in_range(expected: float, measured: float, tolerance: float = OUT_OF_RANGE_TOLERANCE) -> bool:
    """The reference's band test: strict, with ±tolerance and ±1.0 (§6.4)."""
    upper = expected * (1.0 + tolerance) + ABSOLUTE_MARGIN
    lower = expected * (1.0 - tolerance) - ABSOLUTE_MARGIN
    return lower < measured < upper


@dataclass(frozen=True)
class RangeStatus:
    testing: bool
    hv_ok: bool = True
    current_ok: bool = True
    power_ok: bool = True

    @property
    def ok(self) -> bool:
        return self.hv_ok and self.current_ok and self.power_ok


class RangeChecker:
    def __init__(self, unit: UnitConfig, safety: SafetyConfig):
        self._unit = unit
        self._tolerance = safety.range_tolerance
        self._off_delay_s = safety.hv_off_test_delay_s
        self._switched_at: float | None = None

    def hv_switched(self, now: float) -> None:
        """Record that HV was switched on or off; restarts the HV-off delay."""
        self._switched_at = now

    def check(self, now: float, *, hv_on: bool, expected: DacSetpoints | None,
              kv: float, ua: float) -> RangeStatus:
        """Evaluate one pair of readings.

        expected is the committed setpoints, required when hv_on.
        Do not call this while a setpoint change is in progress.
        """
        if self._switched_at is None:
            self._switched_at = now       # the delay also applies after startup
        if hv_on:
            if expected is None:
                raise ValueError("expected setpoints are required with HV on")
            expected_kv, expected_ua = expected.kv, expected.ua
        elif now - self._switched_at < self._off_delay_s:
            return RangeStatus(testing=False)
        else:
            expected_kv = expected_ua = HV_OFF_EXPECTED
        return RangeStatus(
            testing=True,
            hv_ok=in_range(expected_kv, kv, self._tolerance),
            current_ok=in_range(expected_ua, ua, self._tolerance),
            power_ok=kv * ua < (self._unit.watt_max_w + self._unit.safety_margin_w) * 1000.0,
        )
