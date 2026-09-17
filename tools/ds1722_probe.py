#!/usr/bin/env python3
"""Find the working DS1722 (temperature sensor) transactions on real hardware.

docs/protocol.md §8 reconstructed these transactions; the vendor source in
reference/ (MiniXDlg.cpp, ReadTemperature and SetTempSensor) has the real
ones. See docs/hardware-notes.md for the earlier probe runs.

Clock modes:

  low     clock low when TSCS rises and during the transfer
  high    clock high when TSCS rises and during the transfer
  vendor  clock low when TSCS rises, the low state rewritten twice more,
          then clock high for the transfer; this is the vendor sequence.
          The board inverts the clock ("take TS clock low - this makes
          clock high").

Phase 1 (always, read-only): at each clock rate, for each clock mode,
address opcode (0x10/0x11 bytes, 0x12/0x13 bits) and read opcode (0x20,
0x24), read 3 bytes from address 0x00 (config, temp LSB, temp MSB), 2 from
0x01 and 1 from 0x02. "Read framing" is OK when the address-0 bytes are
valid (config 111xxxxx, a real temperature) and the other read opcode
returned the same bytes or the same bytes one bit early. "Addressing" is
OK when the address-1 and address-2 reads return the matching bytes;
address 0x00 alone cannot show this, since it looks the same shifted.
The vendor read is mode vendor, 0x12, 0x20.

Phase 2 (--write only): for each combination that passes phase 1, write
the config register with each of the four output opcodes (the vendor uses
0x10), twice with different values, reading each back. A successful write
leaves 0xE8 (continuous 12-bit conversion), the vendor's setting.

HV is never enabled and the DACs are never written. Only TSCS is selected
during these transactions; the ADC and DAC chip selects stay deasserted.

    python tools/ds1722_probe.py                     # phase 1 at 1500 and 150 kHz
    python tools/ds1722_probe.py --khz 1500 --write  # phases 1 and 2 at 1500 kHz
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time

from minix import protocol as p
from minix.device import DeviceError, MiniX, decode_temperature_c
from minix.discovery import list_controllers
from minix.transport import FtdiTransport, TransportError

CONFIG_A = p.TS_CONFIG_12BIT_CONTINUOUS   # 0xE8
CONFIG_B = 0xE4                           # 10-bit, continuous
CONVERSION_S = 1.5                        # 12-bit conversion takes up to ~1.2 s
FT2232CD_BASE_KHZ = 12000                 # SCK = base / ((1 + divisor) * 2)

MODES = ("vendor", "low", "high")
OUT_OPS = (0x10, 0x11, 0x12, 0x13)
IN_OPS = (0x20, 0x24)
BYTE_OUT_OPS = (0x10, 0x11)


def divisor_for(khz: float) -> int:
    divisor = round(FT2232CD_BASE_KHZ / (2 * khz)) - 1
    if not 0 <= divisor <= 0xFFFF:
        raise ValueError(f"{khz} kHz is out of range")
    return divisor


def actual_khz(divisor: int) -> float:
    return FT2232CD_BASE_KHZ / ((1 + divisor) * 2)


def adbus(clk_high: bool) -> bytes:
    # HV enables clear, ADC and DAC deselected, data line low.
    state = p.ADBUS_INIT & ~(p.CLK | p.DATA_OUT)
    if clk_high:
        state |= p.CLK
    return bytes([p.SET_ADBUS, state, p.ADBUS_DIRECTION])


def framed(divisor: int, mode: str, body: bytes) -> bytes:
    """Select the DS1722, run body, deselect, restore the default clock rate."""
    at_select = adbus(clk_high=mode == "high")
    during = adbus(clk_high=mode != "low")
    tx = bytes([p.SET_DIVISOR, divisor & 0xFF, divisor >> 8])
    tx += at_select + bytes([p.SET_ACBUS, p.TSCS, p.ACBUS_DIRECTION])
    if mode == "vendor":
        tx += at_select * 2
    tx += during + body
    tx += bytes([p.SET_ACBUS, 0x00, p.ACBUS_DIRECTION]) + during
    return tx + bytes([p.SET_DIVISOR, *p.CLOCK_DIVISOR])


def clock_out(op: int, data: bytes) -> bytes:
    if op in BYTE_OUT_OPS:
        return bytes([op, len(data) - 1, 0x00]) + data
    return b"".join(bytes([op, 0x07, b]) for b in data)


def read_registers(transport, variant, address: int, count: int) -> bytes:
    divisor, mode, out_op, in_op = variant
    body = clock_out(out_op, bytes([address]))
    body += adbus(clk_high=mode != "low")   # the vendor's "INPUTMODE" rewrite
    body += bytes([in_op, count - 1, 0x00])
    return transport.exchange(framed(divisor, mode, body), count)


def write_config(transport, divisor: int, mode: str, out_op: int, value: int) -> None:
    body = clock_out(out_op, bytes([p.TS_CONFIG_WRITE, value]))
    transport.write(framed(divisor, mode, body))


def valid_frame(reg: bytes) -> bool:
    cfg, lsb, msb = reg
    if cfg & 0xE0 != 0xE0:
        return False
    try:
        decode_temperature_c(msb, lsb)
    except DeviceError:
        return False
    return True


def is_one_bit_early(early: bytes, correct: bytes) -> bool:
    """early is correct shifted right by one bit, whatever bit came in on top."""
    return int.from_bytes(early) & 0x7FFFFF == int.from_bytes(correct) >> 1


def describe_config(cfg: int) -> str:
    code = cfg >> 1 & 0x07
    bits = 12 if code & 0x04 else 8 + code
    return (f"{cfg:#04x}: {bits}-bit, 1SHOT={cfg >> 4 & 1}, "
            f"SD={cfg & 1} ({'shutdown' if cfg & 1 else 'converting'})")


def label(variant) -> str:
    divisor, mode, out_op, in_op = variant
    return f"{actual_khz(divisor):7.1f}  {mode:6}  {out_op:#04x}  {in_op:#04x}"


HEADER = "    kHz  mode    out   in  "


def phase1(transport, divisor: int) -> list:
    reads = {}
    for variant in itertools.product((divisor,), MODES, OUT_OPS, IN_OPS):
        reads[variant] = (read_registers(transport, variant, p.TS_CONFIG_READ, 3),
                          read_registers(transport, variant, p.TS_TEMP_LSB, 2),
                          read_registers(transport, variant, p.TS_TEMP_MSB, 1))

    print(HEADER + "  @00       @01    @02  read framing  addressing")
    usable = []
    for variant, (r0, r1, r2) in reads.items():
        _, mode, out_op, in_op = variant
        other = reads[(divisor, mode, out_op, 0x24 if in_op == 0x20 else 0x20)][0]
        # A shifted frame can still look valid (f1 80 0c), so framing is
        # judged against the other read edge: the correct frame is the one
        # the other matches or is a one-bit-early copy of.
        if valid_frame(r0) and (other == r0 or is_one_bit_early(other, r0)):
            framing = "OK"
        elif is_one_bit_early(r0, other):
            framing = "1 bit early"
        else:
            framing = "valid?" if valid_frame(r0) else "bad"
        if framing != "OK":
            addressing = "-"
        elif r1 == r0[1:3] and r2 == r0[2:3]:
            addressing = "OK"
            usable.append((variant, r0))
        elif r1 == r0[0:2]:
            addressing = "0x01 read as 0x00"
        else:
            addressing = "wrong"
        vendor = "  <- vendor read" if (mode, out_op, in_op) == ("vendor", 0x12, 0x20) else ""
        print(f"{label(variant)}  {r0.hex(' ')}  {r1.hex(' ')}  {r2.hex()}   "
              f"{framing:12}  {addressing}{vendor}")
    return usable


def phase2(transport, usable) -> bool:
    print("\n" + HEADER + "  write  read back A/B  result")
    written = None
    for variant, _ in usable:
        divisor, mode, _, _ = variant
        for write_op in OUT_OPS:
            write_config(transport, divisor, mode, write_op, CONFIG_B)
            cfg_b = read_registers(transport, variant, p.TS_CONFIG_READ, 1)[0]
            write_config(transport, divisor, mode, write_op, CONFIG_A)
            cfg_a = read_registers(transport, variant, p.TS_CONFIG_READ, 1)[0]
            ok = cfg_a == CONFIG_A and cfg_b == CONFIG_B
            if ok and written is None:
                written = (variant, write_op)
            print(f"{label(variant)}  {write_op:#04x}  {cfg_a:#04x}/{cfg_b:#04x}      "
                  f"{'OK' if ok else 'bad'}")
    if written is None:
        return False
    # Later opcodes may have misaddressed; rewrite with the first that worked.
    variant, write_op = written
    write_config(transport, variant[0], variant[1], write_op, CONFIG_A)
    time.sleep(CONVERSION_S)
    reg = read_registers(transport, variant, p.TS_CONFIG_READ, 3)
    print(f"\nafter writing: config {describe_config(reg[0])}, "
          f"temperature {decode_temperature_c(reg[2], reg[1]):.4f} °C")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", help="USB serial number (default: first found)")
    ap.add_argument("--khz", default="1500,150",
                    help="comma-separated clock rates to try (default: %(default)s)")
    ap.add_argument("--write", action="store_true",
                    help="also test config writes with the combinations that pass phase 1")
    args = ap.parse_args()
    try:
        divisors = [divisor_for(float(k)) for k in args.khz.split(",")]
    except ValueError as exc:
        ap.error(str(exc))

    serial = args.serial
    if serial is None:
        found = list_controllers()
        if not found:
            print("no Mini-X controller found")
            return 1
        serial = found[0].serial

    try:
        transport = FtdiTransport.open(serial)
    except TransportError as exc:
        print(f"open failed: {exc}")
        return 1
    dev = MiniX(transport)

    try:
        dev.initialize()
        print(f"Mini-X {serial} initialized; HV enables clear\n")
        usable = []
        for divisor in divisors:
            usable += phase1(transport, divisor)
            print()
        if not usable:
            print("no combination reads and addresses correctly at any rate; not writing")
            return 0
        print("reads and addresses correctly:")
        for variant, _ in usable:
            print(f"  {label(variant)}")
        cfg, lsb, msb = usable[0][1]
        print(f"current config {describe_config(cfg)}")
        print(f"temperature register {decode_temperature_c(msb, lsb):.4f} °C "
              "(stale if the sensor is shut down)")
        if args.write and not phase2(transport, usable):
            print("\nno combination wrote the config register")
        return 0
    except (TransportError, DeviceError) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        confirmed = dev.close()
        print(f"closed; enables {'confirmed clear' if confirmed else 'NOT CONFIRMED CLEAR, check the tube'}")


if __name__ == "__main__":
    sys.exit(main())
