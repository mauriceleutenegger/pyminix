"""FTDI transport.

The Transport protocol is the byte pipe between the device layer and the
controller hardware: write an MPSSE command stream, read back an exact
number of bytes. FtdiTransport implements it with pyftdi; the simulator
implements it in software.

This layer handles USB only: opening the channel, latency, timeouts,
entering and leaving MPSSE mode, and purging. The MPSSE command bytes that
set up the pins, including clearing the HV enables, belong to the device
layer (§5).

Reads either return exactly the bytes requested or raise TransportError.
There are no sentinel values.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

from pyftdi.ftdi import Ftdi, FtdiError
from usb.core import USBError

from pyftdi.usbtools import UsbTools

from . import protocol as p
from .discovery import register_usb_ids

log = logging.getLogger(__name__)

# Each attempt waits one latency period (4 ms) for data; a read returns as
# soon as its bytes arrive, so extra attempts cost nothing on success.
_READ_ATTEMPTS = 8


class TransportError(IOError):
    """A USB-level failure: open, write, read or purge."""


class Transport(Protocol):
    @property
    def serial(self) -> str: ...

    def write(self, data: bytes) -> None: ...

    def exchange(self, data: bytes, reply_len: int) -> bytes:
        """Purge, write data, and return exactly reply_len bytes."""
        ...

    def close(self) -> None: ...


class FtdiTransport:
    """Channel A of an FT2232C/D in MPSSE mode."""

    def __init__(self, ftdi: Ftdi, serial: str):
        self._ftdi = ftdi
        self._serial = serial

    @classmethod
    def open(cls, serial: str) -> FtdiTransport:
        """Open the controller with this USB serial number and enter MPSSE mode."""
        register_usb_ids()
        # Forget where the controller was last seen: after a replug it may be
        # at a different USB address, and a stale entry fails with "no such
        # device". Open devices are tracked separately, so this is safe.
        UsbTools.flush_cache()
        ftdi = Ftdi()
        try:
            ftdi.open(p.USB_VID, p.USB_PID, serial=serial, interface=p.USB_INTERFACE)
        except (OSError, ValueError) as exc:  # FtdiError, USBError, "No such device"
            raise TransportError(f"cannot open Mini-X {serial}: {exc}") from exc
        try:
            version = ftdi.device_version
            if version != p.FT2232CD_VERSION:
                raise TransportError(
                    f"Mini-X {serial} reports bcdDevice {version:#06x}, expected "
                    f"{p.FT2232CD_VERSION:#06x} (FT2232C/D)")
            ftdi.set_latency_timer(p.LATENCY_MS)
            ftdi.timeouts = (p.READ_TIMEOUT_MS, p.WRITE_TIMEOUT_MS)
            ftdi.purge_buffers()
            ftdi.set_bitmode(0x00, Ftdi.BitMode.MPSSE)
            # The validated scripts pause here before the first command.
            time.sleep(0.05)
        except (FtdiError, USBError) as exc:
            ftdi.close()
            raise TransportError(f"cannot configure Mini-X {serial}: {exc}") from exc
        except TransportError:
            ftdi.close()
            raise
        log.info("opened Mini-X %s", serial)
        return cls(ftdi, serial)

    @property
    def serial(self) -> str:
        return self._serial

    def write(self, data: bytes) -> None:
        try:
            n = self._ftdi.write_data(data)
        except (FtdiError, USBError) as exc:
            raise TransportError(f"write failed: {exc}") from exc
        if n != len(data):
            raise TransportError(f"short write: {n}/{len(data)} bytes")

    def exchange(self, data: bytes, reply_len: int) -> bytes:
        # Stale bytes in the RX buffer would misalign every later read (§11).
        try:
            self._ftdi.purge_buffers()
        except (FtdiError, USBError) as exc:
            raise TransportError(f"purge failed: {exc}") from exc
        self.write(data)
        try:
            rx = bytes(self._ftdi.read_data_bytes(reply_len, attempt=_READ_ATTEMPTS))
        except (FtdiError, USBError) as exc:
            raise TransportError(f"read failed: {exc}") from exc
        if len(rx) != reply_len:
            raise TransportError(f"short read: {len(rx)}/{reply_len} bytes")
        return rx

    def close(self) -> None:
        """Leave MPSSE mode and release the device.

        Clear the HV enables through the device layer before calling this.
        """
        try:
            self._ftdi.close()  # resets bitmode first
        except (FtdiError, USBError) as exc:
            raise TransportError(f"close failed: {exc}") from exc
        log.info("closed Mini-X %s", self._serial)
