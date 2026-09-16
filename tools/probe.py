#!/usr/bin/env python3
"""
minix_probe.py -- first-contact test for the Amptek Mini-X controller via pyftdi.

SAFETY: never asserts the HV enable bits. MPSSE init writes them CLEARED
(matching OnButtonMpsseOn); teardown re-clears them in a finally block.
Nothing here can energize the tube.

    pip install pyftdi

    python minix_probe.py                    # stages 1-3
    python minix_probe.py --list             # enumerate only, do not open
    python minix_probe.py --url ftdi://...   # skip autodetect
    python minix_probe.py --monitors         # also read HV/current ADCs
    python minix_probe.py --temp             # also read DS1722 (EXPERIMENTAL)
    python minix_probe.py --poll 10          # repeat stage 3 N times
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    from pyftdi.ftdi import Ftdi
except ImportError:
    sys.exit("pyftdi not installed.  pip install pyftdi")

MINIX_VID = 0x0403
MINIX_PID = 0xD058          # custom Amptek PID, confirmed on sn 01300036    

# --- constants from MiniXDlg.cpp, Version C0 --------------------------------

OUTPUTMODE   = 0x7B     # ADBUS direction, 1=out
OUTPUTMODE_H = 0x08     # ACBUS: only ACBUS3 (TSCS) is an output

CLKSTATE     = 0x01
DATASTATE    = 0x02
ADCS         = 0x08
DACS         = 0x10
CTRL_HV_EN_A = 0x20
CTRL_HV_EN_B = 0x40
TSCS         = 0x08     # on ACBUS, ACTIVE HIGH

CLK_BYTES_OUT = 0x10    # CLK_FN in the C source
CLK_BITS_OUT  = 0x13
CLK_BYTES_IN  = 0x20

AD0 = 0xD0              # 1101 -> ADC ch0, HV monitor
AD1 = 0xF0              # 1111 -> ADC ch1, current monitor
TSLSB = 0x01

VREF, SCALE = 4.096, 4096
UA_FACTOR = 50.0


#def hv_factor(serial: int) -> float:
#    return 12.5 if serial >= 1118880 else 10.0

def hv_factor(serial: int) -> float:
    if serial < 1118880:
        raise ValueError(f"serial {serial} predates the 50 kV boards; "
                         "40 kV table not validated on this unit")
    return 12.5


def unpack_adc(rx: bytes) -> int:
    """1 null bit + 12 data MSB-first + 3 trailing == (>> 3)."""
    if len(rx) < 2:
        raise IOError(f"ADC reply too short: {len(rx)} bytes")
    raw = ((rx[0] << 8) | rx[1]) >> 3
    if raw & 0x1000:
        raise IOError(f"ADC framing error: rx={rx[0]:#04x} {rx[1]:#04x}")
    return raw & 0x0FFF


def decode_temp_c(msb: int, lsb: int) -> float:
    raw = (msb << 4) | (lsb >> 4)
    if msb & 0x80:
        raw -= 4096
    return raw * 0.0625


# --- device wrapper ---------------------------------------------------------

class MiniXProbe:
    def __init__(self, ftdi: Ftdi):
        self.f = ftdi
        self.low = 0x00
        self.high = 0x00

    # -- transport --

    def _w(self, data: bytes) -> None:
        n = self.f.write_data(data)
        if n != len(data):
            raise IOError(f"short write: {n}/{len(data)}")

    def _r(self, count: int) -> bytes:
        rx = self.f.read_data_bytes(count, attempt=4)
        if len(rx) < count:
            raise IOError(f"short read: {len(rx)}/{count}")
        return bytes(rx)

    def _set_low(self) -> bytes:
        return bytes([0x80, self.low, OUTPUTMODE])

    def _set_high(self) -> bytes:
        return bytes([0x82, self.high, OUTPUTMODE_H])

    # -- init / teardown --

    def init_mpsse(self) -> None:
        """Mirrors OnButtonMpsseOn: HV enables CLEARED, then clk divisor."""
        self.f.set_latency_timer(4)
        self.f.purge_buffers()
        self.f.set_bitmode(0x00, Ftdi.BitMode.MPSSE)
        time.sleep(0.05)

        # high byte first: TSCS low (deasserted, it is active high)
        self.high = OUTPUTMODE_H
        self.high &= ~TSCS
        # low byte: all high per the original (0xFB), then clear HV enables
        self.low = 0xFB
        self.low &= ~CTRL_HV_EN_A
        self.low &= ~CTRL_HV_EN_B

        self._w(self._set_high() + self._set_low())
        self._w(bytes([0x86, 0x03, 0x00]))   # SetClkDivisor -> ~1.5 MHz

    def failsafe(self) -> None:
        """Clear HV enables. Best effort; never raises."""
        try:
            self.low &= ~CTRL_HV_EN_A
            self.low &= ~CTRL_HV_EN_B
            self._w(self._set_low())
        except Exception as e:
            print(f"  !! failsafe write failed: {e}", file=sys.stderr)

    # -- reads --

    def read_gpio(self) -> tuple[int, int]:
        self._w(bytes([0x81]))
        p0 = self._r(1)[0]
        self._w(bytes([0x83]))
        p1 = self._r(1)[0]
        return p0, p1

    def read_adc(self, channel: int) -> int:
        """Transcribed from OnButtonReadAdc / OnButtonReadAdc1."""
        tx = bytearray([0x86, 0x03, 0x00])
        self.low &= ~ADCS          # assert, active low
        self.low &= ~CLKSTATE
        tx += self._set_low()
        tx += bytes([CLK_BITS_OUT, 0x03, channel])   # 4-bit control nibble
        tx += self._set_low()                        # INPUTMODE == OUTPUTMODE in C0
        tx += bytes([CLK_BYTES_IN, 0x01, 0x00])      # 2 bytes in
        self.low |= ADCS           # deassert
        tx += self._set_low()
        self._w(bytes(tx))
        return unpack_adc(self._r(2))

    def read_temp_c(self) -> float:
        """EXPERIMENTAL: the address clock-out was unreadable in the source
        I was given, so the TSLSB write below is a reconstruction."""
        tx = bytearray([0x86, 0x03, 0x00])
        self.low &= ~CLKSTATE      # clock low BEFORE asserting CE
        self.low &= ~DATASTATE
        tx += self._set_low()
        self.high |= TSCS          # assert, ACTIVE HIGH
        tx += self._set_high()
        tx += bytes([CLK_BYTES_OUT, 0x00, 0x00, TSLSB])   # 1 byte: address
        tx += bytes([CLK_BYTES_IN, 0x01, 0x00])           # 2 bytes: LSB, MSB
        self.high &= ~TSCS         # deassert
        tx += self._set_high()
        self._w(bytes(tx))
        rx = self._r(2)
        return decode_temp_c(rx[1], rx[0])   # LSB first, per GetTemp(rx[1],rx[0])


# --- stages ----------------------------------------------------------------

def stage1_enumerate() -> list[tuple]:
    print("=" * 68)
    print("STAGE 1: enumerate FTDI devices")
    print("=" * 68)
    devs = Ftdi.list_devices()
    if not devs:
        print("  no FTDI devices found.")
        print(troubleshooting())
        return []
    for i, (desc, iface) in enumerate(devs):
        print(f"  [{i}] vid={desc.vid:#06x} pid={desc.pid:#06x} "
              f"bus={desc.bus} addr={desc.address}")
        print(f"      serial={desc.sn!r}  desc={desc.description!r}  iface={iface}")
    return devs


def pick_url(devs: list[tuple]) -> tuple[str, str]:
    """Return (url, serial_string). Prefers a device whose serial is numeric."""
    for desc, _ in devs:
        sn = desc.sn or ""
        if sn[:8].isdigit():
            return (f"ftdi://{desc.vid:#06x}:{desc.pid:#06x}:{sn}/1", sn)
    desc, _ = devs[0]
    print(f"  ! no numeric serial found; falling back to {desc.sn!r}")
    return (f"ftdi://{desc.vid:#06x}:{desc.pid:#06x}:{desc.sn}/1", desc.sn or "")


def stage2_identify(sn: str) -> int:
    print()
    print("=" * 68)
    print("STAGE 2: identify")
    print("=" * 68)
    print(f"  raw serial     : {sn!r}")
    head = sn[:8]
    try:
        serial = int("".join(c for c in head if c.isdigit()) or 0)
    except ValueError:
        serial = 0
    print(f"  Mini-X serial  : {serial}")
    if serial == 0:
        print("  ! could not parse a numeric serial; board table is a GUESS")
    print(f"  isNSI          : {serial > 9999}")
    print(f"  is50kv         : {serial >= 1118880}")
    f = hv_factor(serial)
    print(f"  HV factor      : {f} kV/V   -> HV max {50.0 if f == 12.5 else 40.0} kV")
    print(f"  current factor : {UA_FACTOR} uA/V -> I max 200 uA")
    return serial


def stage3_gpio(dev: MiniXProbe, serial: int, args) -> None:
    print()
    print("=" * 68)
    print("STAGE 3: GPIO / interlock" + (" + monitors" if args.monitors else ""))
    print("=" * 68)
    kvf = hv_factor(serial)

    for n in range(args.poll):
        p0, p1 = dev.read_gpio()
        interlock = bool(p1 & 0x01)      # p10, 1=closed
        hv_en = bool(p0 & 0x20)          # p05, CtrlHvEn readback
        rdy = bool(p0 & 0x80)            # p07, MonMiniXRdy
        line = (f"  [{n:>3}] ADBUS={p0:08b} ACBUS={p1:08b}  "
                f"interlock={'CLOSED' if interlock else 'OPEN':<6} "
                f"hv_en={int(hv_en)} rdy={int(rdy)}")
        print(line)

        if not interlock:
            print("       ^ interlock OPEN -- HV would be inhibited")
        if hv_en:
            print("       ^ WARNING: HV enable bit reads back SET. Unexpected;")
            print("         this script never asserts it. Investigate before use.")

        if args.monitors:
            try:
                c0 = dev.read_adc(AD0)
                c1 = dev.read_adc(AD1)
                kv = c0 / SCALE * VREF * kvf
                ua = c1 / SCALE * VREF * UA_FACTOR
                print(f"       ch0={c0:>4} ({c0/SCALE*VREF:.4f} V) -> {kv:6.2f} kV")
                print(f"       ch1={c1:>4} ({c1/SCALE*VREF:.4f} V) -> {ua:6.2f} uA")
                if not interlock or not hv_en:
                    if kv > 2.0 or ua > 5.0:
                        print("       ^ NOTE: nonzero monitor with HV off. Could be")
                        print("         offset, or a channel mapping error.")
            except IOError as e:
                print(f"       ADC read failed: {e}")

        if args.temp:
            try:
                print(f"       temp={dev.read_temp_c():.2f} C  (EXPERIMENTAL)")
            except IOError as e:
                print(f"       temp read failed: {e}")

        if n + 1 < args.poll:
            time.sleep(args.interval)


def troubleshooting() -> str:
    return """
  Troubleshooting:
    Linux : needs libusb access. Either run with sudo to test, or install
            a udev rule:
              SUBSYSTEM=="usb", ATTR{idVendor}=="0403", MODE="0666"
            in /etc/udev/rules.d/99-ftdi.rules, then: sudo udevadm control
            --reload-rules && sudo udevadm trigger
            Also unbind the kernel driver if it claimed the device:
              sudo rmmod ftdi_sio
    macOS : the Apple FTDI driver may hold the device.
              sudo kextunload -b com.apple.driver.AppleUSBFTDI
            (On recent macOS this is a dext and may not need unloading.)
    Windows: pyftdi needs the WinUSB/libusb driver, NOT the FTDI VCP/D2XX
            driver. Use Zadig to replace the driver for this device --
            note this will stop the Amptek software from seeing it until
            you switch back.
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="enumerate only")
    ap.add_argument("--url", help="explicit pyftdi URL")
    ap.add_argument("--monitors", action="store_true", help="read HV/I ADCs")
    ap.add_argument("--temp", action="store_true", help="read DS1722 (experimental)")
    ap.add_argument("--poll", type=int, default=1, help="repeat stage 3 N times")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between polls")
    ap.add_argument("--vid", type=lambda s: int(s, 0), default=MINIX_VID)
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=MINIX_PID)
    args = ap.parse_args()

    print()
    print("  Mini-X probe -- READ ONLY, never enables high voltage")
    print()

    if args.pid:
        if args.vid != 0x0403:
            try:
                Ftdi.add_custom_vendor(args.vid, "custom")
            except ValueError:
                pass
        try:
            Ftdi.add_custom_product(args.vid, args.pid, "minix")
            print(f"  registered custom {args.vid:#06x}:{args.pid:#06x}")
        except ValueError:
            pass
    

    devs = stage1_enumerate()
    if not devs:
        return 1
    if args.list:
        return 0

    if args.url:
        url, sn = args.url, ""
        print(f"\n  using explicit URL: {url}")
    else:
        url, sn = pick_url(devs)
        print(f"\n  selected: {url}")

    serial = stage2_identify(sn) if sn else 0

    print()
    print("=" * 68)
    print("STAGE 2b: open + MPSSE init")
    print("=" * 68)

    ftdi = Ftdi()
    dev = None
    try:
        ftdi.open_from_url(url)
        print("  opened OK")
        print(f"  device type    : {ftdi.device_version:#06x}")
        dev = MiniXProbe(ftdi)
        dev.init_mpsse()
        print("  MPSSE enabled, HV enables written CLEARED")
        print(f"  ADBUS state    : {dev.low:08b}  dir {OUTPUTMODE:08b}")
        print(f"  ACBUS state    : {dev.high:08b}  dir {OUTPUTMODE_H:08b}")
        print("  clock divisor  : 0x0003 -> ~1.5 MHz")

        stage3_gpio(dev, serial, args)

        print()
        print("  probe complete.")
        return 0

    except Exception as e:
        print(f"\n  FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        print(troubleshooting(), file=sys.stderr)
        return 1

    finally:
        if dev is not None:
            print("\n  teardown: clearing HV enables...")
            dev.failsafe()
        try:
            ftdi.set_bitmode(0x00, Ftdi.BitMode.RESET)
            ftdi.close()
            print("  closed.")
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
