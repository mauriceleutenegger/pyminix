"""Power indicator bands (§6.4).

The UI maps the bands to colours (white, green, yellow, red). A maximal
setpoint shows CAUTION: the band starts two safety margins below the
rating, while the setpoint limit is one margin below it.
"""

from __future__ import annotations

import enum
import math

from ..config import UnitConfig

IDLE_BELOW_MW = 10.0     # mwSafe; absolute, does not scale with the rating


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
