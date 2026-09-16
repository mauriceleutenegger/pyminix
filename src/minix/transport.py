"""FTDI transport.

Transport protocol and FtdiTransport: open, MPSSE init with the HV enables
cleared (§5), sync check (§5.1), purge-then-read, teardown. Reads either
return the requested bytes or raise; there are no sentinel values.
"""
