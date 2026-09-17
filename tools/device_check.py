#!/usr/bin/env python3
"""Read-only hardware check of minix.transport and minix.device.

Never writes the DACs and never enables HV. It opens the controller,
initializes it (which clears the HV enables), checks MPSSE sync, reads GPIO
and both ADC channels, and runs failsafe() on exit.

    python tools/device_check.py                 # first controller found
    python tools/device_check.py --serial 01300036
    python tools/device_check.py --polls 10
    python tools/device_check.py --temp          # also configure and read the DS1722

Expected with HV off (docs/protocol.md §7.1, §10.2): ADBUS 00011111 on the
first read (bits 0-1 then follow the clock and data levels the previous
transaction left), ACBUS 00000101, and both ADC channels within a few
dozen counts of zero. ADC framing errors are counted rather than fatal.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from minix import protocol as p
from minix.device import DeviceError, FramingError, MiniX
from minix.discovery import list_controllers
from minix.transport import FtdiTransport, TransportError


def show_adc(dev: MiniX, channel: int) -> tuple[str, bool]:
    try:
        r = dev.read_adc(channel)
    except FramingError as exc:
        return f"FRAMING ERROR ({exc})", False
    return f"{r.counts:4d} ({r.volts:.4f} V, raw {r.raw.hex()})", True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", help="USB serial number (default: first found)")
    ap.add_argument("--polls", type=int, default=3)
    ap.add_argument("--interval", type=float, default=0.5, help="seconds between polls")
    ap.add_argument("--temp", action="store_true", help="configure and read the DS1722")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="  %(levelname)s %(name)s: %(message)s")

    serial = args.serial
    if serial is None:
        found = list_controllers()
        if not found:
            print("no Mini-X controller found")
            return 1
        for c in found:
            print(f"found {c.serial!r} ({c.description!r}, bus {c.bus} addr {c.address})")
        serial = found[0].serial

    try:
        dev = MiniX(FtdiTransport.open(serial))
    except TransportError as exc:
        print(f"open failed: {exc}")
        return 1

    try:
        dev.initialize()
        print("initialized; MPSSE sync OK")
        if args.temp:
            dev.configure_temperature_sensor()
            time.sleep(1.5)  # a 12-bit conversion takes up to ~1.2 s
        framing_errors = 0
        for i in range(args.polls):
            if i:
                time.sleep(args.interval)
            gpio = dev.read_gpio()
            hv, hv_ok = show_adc(dev, p.ADC_HV)
            cur, cur_ok = show_adc(dev, p.ADC_CURRENT)
            framing_errors += (not hv_ok) + (not cur_ok)
            print(f"[{i}] ADBUS {gpio.adbus:08b} ACBUS {gpio.acbus:08b}  "
                  f"interlock {'CLOSED' if gpio.interlock_closed else 'OPEN'}  "
                  f"enables {'clear' if gpio.hv_disabled else 'SET'}  "
                  f"MONX {int(gpio.tube_ready)}  "
                  f"CS idle {'yes' if gpio.chip_selects_idle else 'NO'}")
            print(f"    ADC HV {hv}  current {cur}")
            if not gpio.hv_disabled:
                print("    !! enable bits read back SET; this script never sets them")
            if args.temp:
                try:
                    print(f"    temperature {dev.read_temperature_c():.4f} °C")
                except DeviceError as exc:
                    print(f"    temperature read failed: {exc}")
        print(f"ADC framing errors: {framing_errors} of {2 * args.polls} readings")
        return 0
    except (TransportError, DeviceError) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        confirmed = dev.close()
        print(f"closed; enables {'confirmed clear' if confirmed else 'NOT CONFIRMED CLEAR, check the tube'}")


if __name__ == "__main__":
    sys.exit(main())
