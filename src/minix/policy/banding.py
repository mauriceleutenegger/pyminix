"""Power indicator bands (§6.4).

The UI maps the bands to colours (white, green, yellow, red). A maximal
setpoint shows CAUTION: the band starts two safety margins below the
rating, while the setpoint limit is one margin below it.

power_band() is the reference's banding. PowerBandTracker adds hysteresis
for use with averaged power: at full power the measured average sits
within a few tens of mW of the caution threshold (§10.8), so without it
the indicator flickers. A higher band is entered at once; a lower band
only once the power is BAND_HYSTERESIS_MW below the current band's
threshold.
"""

from __future__ import annotations

import enum
import math

from ..config import UnitConfig

IDLE_BELOW_MW = 10.0     # mwSafe; absolute, does not scale with the rating
BAND_HYSTERESIS_MW = 100.0


class PowerBand(enum.Enum):
    IDLE = "idle"
    NORMAL = "normal"
    CAUTION = "caution"
    DANGER = "danger"


def power_band(power_mw: float, unit: UnitConfig) -> PowerBand:
    if math.isnan(power_mw):
        raise ValueError("power is NaN")
    caution_mw = (unit.watt_max_w - 2.0 * unit.safety_margin_w) * 1000.0
    if power_mw < IDLE_BELOW_MW:
        return PowerBand.IDLE
    if power_mw < caution_mw:
        return PowerBand.NORMAL
    if power_mw < unit.rated_mw:
        return PowerBand.CAUTION
    return PowerBand.DANGER


_ORDER = [PowerBand.IDLE, PowerBand.NORMAL, PowerBand.CAUTION, PowerBand.DANGER]


def _lower_threshold_mw(band: PowerBand, unit: UnitConfig) -> float:
    if band is PowerBand.NORMAL:
        return IDLE_BELOW_MW
    if band is PowerBand.CAUTION:
        return (unit.watt_max_w - 2.0 * unit.safety_margin_w) * 1000.0
    if band is PowerBand.DANGER:
        return unit.rated_mw
    return -math.inf


class PowerBandTracker:
    def __init__(self, unit: UnitConfig, hysteresis_mw: float = BAND_HYSTERESIS_MW):
        self._unit = unit
        self._hysteresis_mw = hysteresis_mw
        self.band: PowerBand | None = None

    def reset(self) -> None:
        self.band = None

    def update(self, power_mw: float) -> PowerBand:
        band = power_band(power_mw, self._unit)
        if self.band is None or _ORDER.index(band) >= _ORDER.index(self.band):
            self.band = band
            return band
        # Step down one band at a time, each with its own threshold.
        while self.band is not PowerBand.IDLE:
            lower = _lower_threshold_mw(self.band, self._unit)
            if power_mw >= lower - min(self._hysteresis_mw, lower / 2):
                break
            self.band = _ORDER[_ORDER.index(self.band) - 1]
        return self.band
