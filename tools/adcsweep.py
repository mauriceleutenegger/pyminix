#!/usr/bin/env python3
"""
minix_adcsweep.py -- determine whether the Mini-X ADC read path works, and
which MPSSE clock edges the DAC and ADC actually require.

  Stage A (default, read-only): sweep read opcodes with HV OFF. Baseline.
  Stage B (--enable-hv):        set a setpoint, ENERGIZE, re-sweep, compare.

*** STAGE B ENERGIZES THE X-RAY TUBE. Default 15 kV / 10 uA = 150 mW of a
*** 4 W limit. HV is disabled in a finally block on every exit path.

    python minix_adcsweep.py
    python minix_adcsweep.py --enable-hv
    python minix_adcsweep.py --enable-hv --kv 30 --ua 50
    python minix_adcsweep.py --enable-hv --dac-opcode 0x11
"""

from __future__ import annotations

import argparse
import sys
import time

from pyftdi.ftdi import Ftdi

MINIX_VID, MINIX_PID = 0x0403, 0xD058      # confirmed: sn 01300036, FT2232C/D

OUTPUTMODE, OUTPUTMODE_H = 0x7B, 0x08
CLKSTATE, DATASTATE = 0x01, 0x02
ADCS, DACS = 0x08, 0x10
CTRL_HV_EN_A, CTRL_HV_EN_B = 0x20, 0x40
DACA, DACB = 0x18, 0x19                    # AD5623R: write-and-update ch A/B
AD0, AD1 = 0xD0, 0xF0

VREF, SCALE = 4.096, 4096
HV_FACTOR, UA_FACTOR = 12.5, 50.0          # is50kv board
HV_MIN, HV_MAX = 10.0, 50.0
UA_MIN, UA_MAX = 5.0, 200.0
WATT_MAX = 4.0

# Byte-mode read opcodes. NOTE: the 0x22/0x26 alternatives commented out in
# the Amptek source are BIT-mode reads, which take one length byte, not two.
# As written there ("0x22, 0x01, 0x00") the trailing 0x00 would be parsed as
# a command, and 0x00 is invalid -- so those lines were never valid. Only
# 0x20 (sample rising) and 0x24 (sample falling) are meaningful here.
READ_OPCODES = {
    0x20: "bytes in, sample on RISING edge",
    0x24: "bytes in, sample on FALLING edge",
}

# Bit-mode out opcodes for the 4-bit ADC control nibble.
NIBBLE_OPCODES = {
    0x13: "bits out, clock on FALLING edge (original)",
    0x12: "bits out, clock on RISING edge",
}


def unpack(rx: bytes) -> tuple[int, bool]:
    """Return (counts, framing_ok). 1 null + 12 data + 3 trailing == >> 3."""
    if len(rx) < 2:
        raise IOError(f"short ADC reply: {len(rx)}")
    raw = ((rx[0] << 8) | rx[1]) >> 3
    return raw & 0x0FFF, not (raw & 0x1000)


class MiniX:
    def __init__(self, ftdi: Ftdi, dac_opcode: int = 0x10):
        self.f = ftdi
        self.low = 0x00
        self.high = 0x00
        self.dac_opcode = dac_opcode
        self.hv_on = False

    # -- transport --

    def _w(self, data: bytes) -> None:
        n = self.f.write_data(data)
        if n != len(data):
            raise IOError(f"short write {n}/{len(data)}")

    def _r(self, n: int) -> bytes:
        rx = self.f.read_data_bytes(n, attempt=4)
        if len(rx) < n:
            raise IOError(f"short read {len(rx)}/{n}")
        return bytes(rx)

    def _lo(self) -> bytes:
        return bytes([0x80, self.low, OUTPUTMODE])

    def _hi(self) -> bytes:
        return bytes([0x82, self.high, OUTPUTMODE_H])

    def check_mpsse(self) -> None:
        """Send a bad command; MPSSE must echo 0xFA + the offending byte."""
        self.f.purge_buffers()
        self._w(bytes([0xAB]))
        time.sleep(0.05)
        rx = bytes(self.f.read_data_bytes(2, attempt=4))
        if len(rx) >= 2 and rx[0] == 0xFA:
            print(f"  MPSSE sync OK (echo {rx[0]:#04x} {rx[1]:#04x})")
        else:
            print(f"  !! MPSSE sync FAILED, got {rx!r} -- reads are suspect")

    # -- init --

    def init(self) -> None:
        self.f.set_latency_timer(4)
        self.f.purge_buffers()
        self.f.set_bitmode(0x00, Ftdi.BitMode.MPSSE)
        time.sleep(0.05)
        self.high = OUTPUTMODE_H & ~0x00
        self.high &= ~0x08                      # TSCS deasserted (active high)
        self.low = 0xFB & ~CTRL_HV_EN_A & ~CTRL_HV_EN_B
        self._w(self._hi() + self._lo())
        self._w(bytes([0x86, 0x03, 0x00]))
        self.check_mpsse()

    # -- DAC --

    def set_dac(self, channel: int, counts: int) -> None:
        if not 0 <= counts <= 0xFFF:
            raise ValueError(f"counts {counts} out of 12-bit range")
        bhi, blo = (counts & 0x0FF0) >> 4, (counts & 0x000F) << 4
        tx = bytearray()
        self.low &= ~DACS
        self.low |= CLKSTATE                    # per the original: CLK high
        tx += self._lo()
        tx += bytes([self.dac_opcode, 0x02, 0x00, channel, bhi, blo])
        self.low |= DACS
        tx += self._lo()
        self._w(bytes(tx))

    def set_kv(self, kv: float) -> int:
        counts = int(kv / HV_FACTOR / VREF * SCALE)
        self.set_dac(DACA, counts)
        return counts

    def set_ua(self, ua: float) -> int:
        counts = int(ua / UA_FACTOR / VREF * SCALE)
        self.set_dac(DACB, counts)
        return counts

    # -- ADC --

    def read_adc(self, channel: int, read_op: int = 0x20,
                 nibble_op: int = 0x13) -> tuple[int, bool, bytes]:
        self.f.purge_buffers()                  # critical: avoid stale bytes
        tx = bytearray([0x86, 0x03, 0x00])
        self.low &= ~ADCS
        self.low &= ~CLKSTATE
        tx += self._lo()
        tx += bytes([nibble_op, 0x03, channel])
        tx += self._lo()
        tx += bytes([read_op, 0x01, 0x00])
        self.low |= ADCS
        tx += self._lo()
        self._w(bytes(tx))
        rx = self._r(2)
        counts, ok = unpack(rx)
        return counts, ok, rx

    # -- GPIO --

    def gpio(self) -> tuple[int, int]:
        self.f.purge_buffers()
        self._w(bytes([0x81]))
        p0 = self._r(1)[0]
        self._w(bytes([0x83]))
        p1 = self._r(1)[0]
        return p0, p1

    def interlock_closed(self) -> bool:
        return bool(self.gpio()[1] & 0x01)

    # -- HV --

    def hv_enable(self) -> None:
        if not self.interlock_closed():
            raise RuntimeError("interlock OPEN; refusing to enable HV")
        self.low |= CTRL_HV_EN_A
        self.low |= CTRL_HV_EN_B
        self._w(self._lo())
        self.hv_on = True

    def hv_disable(self) -> None:
        """DACs to zero FIRST, then clear enables. Order is deliberate."""
        try:
            self.set_dac(DACA, 0)
            time.sleep(0.2)
            self.set_dac(DACB, 0)
            time.sleep(0.1)
        except Exception as e:
            print(f"  !! DAC zeroing failed during shutdown: {e}", file=sys.stderr)
        self.low &= ~CTRL_HV_EN_A
        self.low &= ~CTRL_HV_EN_B
        try:
            self._w(self._lo())
            self.hv_on = False
        except Exception as e:
            print(f"  !! HV DISABLE WRITE FAILED: {e}", file=sys.stderr)
            print("  !! CHECK THE TUBE PHYSICALLY.", file=sys.stderr)


def sweep(dev: MiniX, label: str) -> dict:
    """Try every read/nibble opcode combination on both channels."""
    print(f"\n  --- opcode sweep: {label} ---")
    results = {}
    for nib_op, nib_desc in NIBBLE_OPCODES.items():
        for rd_op, rd_desc in READ_OPCODES.items():
            row = []
            for name, ch in (("ch0/HV", AD0), ("ch1/I", AD1)):
                try:
                    counts, ok, rx = dev.read_adc(ch, rd_op, nib_op)
                    flag = "" if ok else " FRAMING!"
                    volts = counts / SCALE * VREF
                    row.append(f"{name}={counts:>4} ({volts:.4f} V) "
                               f"raw={rx[0]:02X}{rx[1]:02X}{flag}")
                except IOError as e:
                    row.append(f"{name}=ERR({e})")
            key = (nib_op, rd_op)
            results[key] = row
            print(f"    nibble={nib_op:#04x} read={rd_op:#04x}  {row[0]}")
            print(f"    {'':>24}  {row[1]}")
    print(f"    (nibble opcodes: {NIBBLE_OPCODES})")
    print(f"    (read opcodes:   {READ_OPCODES})")
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enable-hv", action="store_true",
                    help="ENERGIZE the tube for stage B")
    ap.add_argument("--kv", type=float, default=15.0)
    ap.add_argument("--ua", type=float, default=10.0)
    ap.add_argument("--dwell", type=float, default=3.0,
                    help="seconds to settle after enabling HV")
    ap.add_argument("--dac-opcode", type=lambda s: int(s, 0), default=0x10,
                    help="0x10 (default, per source) or 0x11")
    args = ap.parse_args()

    if args.enable_hv:
        if not (HV_MIN <= args.kv <= HV_MAX):
            return _die(f"kv {args.kv} outside {HV_MIN}-{HV_MAX}")
        if not (UA_MIN <= args.ua <= UA_MAX):
            return _die(f"ua {args.ua} outside {UA_MIN}-{UA_MAX}")
        mw = args.kv * args.ua
        if mw > WATT_MAX * 1000:
            return _die(f"{mw:.0f} mW exceeds {WATT_MAX * 1000:.0f} mW limit")
        print()
        print("  " + "!" * 62)
        print("  !! STAGE B WILL ENERGIZE THE X-RAY TUBE")
        print(f"  !! setpoint: {args.kv} kV, {args.ua} uA  ({mw:.0f} mW)")
        print("  !! confirm the tube is in a radiologically safe configuration")
        print("  " + "!" * 62)
        try:
            if input("  type ENERGIZE to proceed: ").strip() != "ENERGIZE":
                print("  aborted.")
                return 0
        except (EOFError, KeyboardInterrupt):
            print("\n  aborted.")
            return 0

    try:
        Ftdi.add_custom_product(MINIX_VID, MINIX_PID, "minix")
    except ValueError:
        pass

    url = f"ftdi://{MINIX_VID:#06x}:{MINIX_PID:#06x}:01300036/1"
    ftdi = Ftdi()
    dev = None
    try:
        ftdi.open_from_url(url)
        dev = MiniX(ftdi, dac_opcode=args.dac_opcode)
        dev.init()
        print(f"  opened {url}")
        print(f"  DAC opcode: {args.dac_opcode:#04x}")

        p0, p1 = dev.gpio()
        print(f"  ADBUS={p0:08b} ACBUS={p1:08b} "
              f"interlock={'CLOSED' if p1 & 0x01 else 'OPEN'}")

        # ---- STAGE A: baseline, HV off ----
        print("\n" + "=" * 68)
        print("STAGE A: HV OFF baseline")
        print("=" * 68)
        base = sweep(dev, "HV off")

        all_zero = all("=   0 " in r for row in base.values() for r in row)
        if all_zero:
            print("\n  All combinations read 0x000.")
            print("  Consistent with EITHER a dead read path OR monitors that")
            print("  hard-zero when HV is disabled. Stage B discriminates.")
        else:
            print("\n  Some combination returned NONZERO with HV off.")
            print("  The read path works; that combination is likely correct.")

        if not args.enable_hv:
            print("\n  Stage A only. Re-run with --enable-hv to discriminate.")
            return 0

        # ---- STAGE B: energized ----
        print("\n" + "=" * 68)
        print("STAGE B: energized")
        print("=" * 68)

        # Per the original: when raising voltage, set CURRENT first.
        c_ua = dev.set_ua(args.ua)
        print(f"  current DAC <- {c_ua} counts ({args.ua} uA)")
        time.sleep(0.2)
        c_kv = dev.set_kv(args.kv)
        print(f"  HV DAC      <- {c_kv} counts ({args.kv} kV)")
        time.sleep(0.2)

        print("  enabling HV...")
        dev.hv_enable()
        print(f"  HV ENABLED. settling {args.dwell} s...")
        time.sleep(args.dwell)

        p0, p1 = dev.gpio()
        print(f"  ADBUS={p0:08b} ACBUS={p1:08b}  "
              f"hv_en={int(bool(p0 & 0x20))} rdy={int(bool(p0 & 0x80))}")

        live = sweep(dev, f"HV ON at {args.kv} kV / {args.ua} uA")

        # ---- verdict ----
        print("\n" + "=" * 68)
        print("VERDICT")
        print("=" * 68)
        winners = []
        for key, row in live.items():
            if not any("=   0 " in r for r in row) and not any("ERR" in r for r in row):
                nib_op, rd_op = key
                winners.append(key)
                print(f"  nibble={nib_op:#04x} read={rd_op:#04x} -> both channels nonzero")
                for r in row:
                    print(f"        {r}")

        if not winners:
            print("  No combination produced nonzero readings while energized.")
            print("  The read path is broken, not merely idle-zero. Next steps:")
            print("    - scope SCLK and the ADC data-out pin during a read")
            print("    - verify the DAC actually moved (measure the control")
            print("      voltage at the tube connector); if not, try")
            print("      --dac-opcode 0x11")
        else:
            print(f"\n  {len(winners)} working combination(s).")
            print("  Cross-check against the expected setpoint:")
            print(f"    expected ~{args.kv} kV and ~{args.ua} uA")
            print("  Pick the combination whose values match; that is the")
            print("  correct edge pair. If values are nonzero but wrong by a")
            print("  large factor, suspect channel mapping (AD0 vs AD1).")

        return 0

    except Exception as e:
        print(f"\n  FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    finally:
        if dev is not None:
            print("\n  teardown: DACs to zero, then HV disable...")
            dev.hv_disable()
            p0, _ = dev.gpio()
            en = bool(p0 & 0x20) or bool(p0 & 0x40)
            print(f"  enable bits readback: {'STILL SET -- CHECK TUBE' if en else 'clear'}")
        try:
            ftdi.set_bitmode(0x00, Ftdi.BitMode.RESET)
            ftdi.close()
            print("  closed.")
        except Exception:
            pass


def _die(msg: str) -> int:
    print(f"  refusing: {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
