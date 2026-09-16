"""Unit configuration.

UnitConfig, loaded from TOML and keyed by serial number. Derives isNSI and
is50kv from the serial (§2.1). The power rating has no default: a unit
without watt_max_w in {4.0, 10.0} is a configuration error (§6.1.1, §9.1).
"""
