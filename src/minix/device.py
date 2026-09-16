"""Peripheral transactions.

MiniX: DAC write (§6.2), ADC read (§6.3), GPIO read (§7.1), HV enable
(§7.2), temperature (§8), failsafe. Knows MPSSE framing; knows nothing
about limits or sequencing. Enable bits change only in set_hv_enable() and
failsafe(); no other transaction alters them.
"""
