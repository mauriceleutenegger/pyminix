"""Setpoint clamping and the wattage limit (§9.1).

clamp_setpoints() returns a Commitment listing the committed values and
every adjustment made, so the UI can show what changed. Excess power is
always taken from the current, never the voltage.
"""
