#!/usr/bin/env python3
"""
minix_findpid.py -- locate the Mini-X's real VID/PID and register it with pyftdi.
Read-only; opens nothing.
"""

import sys

import usb.core
import usb.backend.libusb1
from pyftdi.ftdi import Ftdi

FTDI_VID = 0x0403

backend = usb.backend.libusb1.get_backend()
if backend is None:
    sys.exit("No libusb backend.  brew install libusb")

print("=== all USB devices libusb can see ===")
candidates = []
for d in usb.core.find(find_all=True, backend=backend):
    strings = {}
    for attr in ("manufacturer", "product", "serial_number"):
        try:
            strings[attr] = getattr(d, attr)
        except Exception:
            strings[attr] = None

    blob = " ".join(str(v) for v in strings.values() if v).lower()
    interesting = (
        d.idVendor == FTDI_VID
        or "ftdi" in blob
        or "amptek" in blob
        or "mini" in blob
    )

    if interesting:
        candidates.append((d, strings))
        print(f"  * {d.idVendor:#06x}:{d.idProduct:#06x} "
              f"bus={d.bus} addr={d.address}   <<< candidate")
        for k, v in strings.items():
            print(f"        {k}: {v!r}")
    else:
        print(f"    {d.idVendor:#06x}:{d.idProduct:#06x}")

if not candidates:
    sys.exit("\nNo FTDI-like device found. Check power/cable, and confirm the "
             "Amptek software can still see it.")

print("\n=== registering candidates with pyftdi ===")
for d, _ in candidates:
    if d.idVendor != FTDI_VID:
        try:
            Ftdi.add_custom_vendor(d.idVendor, "custom")
            print(f"  registered vendor {d.idVendor:#06x}")
        except ValueError:
            pass
    try:
        Ftdi.add_custom_product(d.idVendor, d.idProduct, "minix")
        print(f"  registered product {d.idVendor:#06x}:{d.idProduct:#06x}")
    except ValueError as e:
        print(f"  {d.idVendor:#06x}:{d.idProduct:#06x} already known ({e})")

print("\n=== pyftdi enumeration after registration ===")
devs = Ftdi.list_devices()
if not devs:
    print("  still empty -- registration did not help; see notes.")
else:
    for desc, iface in devs:
        print(f"  {desc.vid:#06x}:{desc.pid:#06x} sn={desc.sn!r} "
              f"desc={desc.description!r} iface={iface}")
        print(f"    URL: ftdi://{desc.vid:#06x}:{desc.pid:#06x}:{desc.sn}/1")

print("\n=== pyftdi's own view ===")
try:
    Ftdi.show_devices()
except Exception as e:
    print(f"  show_devices failed: {e}")
