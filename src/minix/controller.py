"""Device controller.

DeviceWorker: the only thread that touches the transport. Takes commands
from a priority queue (emergency stop first), polls GPIO, ADCs and
temperature at independent rates, runs the session state machine, and
reports to the UI through Qt signals (§7, §9.2, §11).
"""
