"""Simulated transport.

SimTransport implements the Transport protocol for tests and for running
the GUI without hardware. Models the DAC registers, ADC offset and
current-channel bias (§10.2, §10.3), MONX lag, the DS1722, and a
switchable interlock.
"""
