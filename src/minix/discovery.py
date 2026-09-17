"""USB discovery.

Registers the custom Amptek PID (0xD058) with pyftdi, lists attached
controllers, and reports each one's serial number from the USB descriptor
(§2). No device I/O happens here.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyftdi.ftdi import Ftdi
from pyftdi.usbtools import UsbTools

from . import protocol as p


@dataclass(frozen=True)
class ControllerInfo:
    serial: str
    description: str
    bus: int | None
    address: int | None


def register_usb_ids() -> None:
    """Tell pyftdi about the custom PID. Safe to call more than once."""
    try:
        Ftdi.add_custom_product(p.USB_VID, p.USB_PID, "minix")
    except ValueError:
        pass  # already registered


def list_controllers() -> list[ControllerInfo]:
    """Return the attached Mini-X controllers, sorted by serial number.

    Call this with no controller open: it clears pyftdi's enumeration
    cache, which otherwise keeps a controller at its old USB address after
    it has been unplugged and plugged back in ("USB Error 19: device may
    have been disconnected").
    """
    register_usb_ids()
    UsbTools.flush_cache()
    try:
        found = Ftdi.list_devices()
    except (OSError, ValueError) as exc:     # FtdiError, USBError, "No such device"
        raise OSError(f"USB enumeration failed: {exc}") from exc
    controllers = [
        ControllerInfo(serial=desc.sn or "", description=desc.description or "",
                       bus=desc.bus, address=desc.address)
        for desc, _ in found
        if desc.vid == p.USB_VID and desc.pid == p.USB_PID
    ]
    return sorted(controllers, key=lambda c: c.serial)
