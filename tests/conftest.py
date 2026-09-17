from __future__ import annotations

import os
from collections import deque

# GUI tests run without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from minix import protocol as p
from minix.device import MiniX
from minix.transport import TransportError
from minix.mpsse import parse


class FakeTransport:
    """Executes MPSSE streams against a minimal model of the pins.

    Output pins read back as driven; inputs come from the attributes below.
    A CLOCK_BYTES_IN with the ADC selected takes its reply from read_replies;
    with the DS1722 selected, it reads ds1722 (config, LSB, MSB) from the
    address last clocked out. A byte clock-out starting with 0x80 writes the
    config register.
    """

    serial = "01300036"

    def __init__(self):
        self.streams: list[bytes] = []
        self.adbus = 0x00
        self.acbus = 0x00
        self.interlock_closed = True
        self.tube_ready = False
        self.read_replies: deque[bytes] = deque()
        self.ds1722 = [p.TS_CONFIG_12BIT_CONTINUOUS, 0x10, 0x19]   # 25.0625 °C
        self.ds1722_address = 0
        self.enable_readback: int | None = None  # force the HV bits on readback
        self.sync_broken = False
        self.acbus_noise = 0x00  # bits 4-7 read back arbitrary values
        self.device_type = 2     # MX50.10, as on sn 01300036
        self.fail_writes = False
        self.closed = False

    def write(self, data: bytes) -> None:
        self._run(data)

    def exchange(self, data: bytes, reply_len: int) -> bytes:
        rx = self._run(data)
        if len(rx) != reply_len:
            raise TransportError(f"short read: {len(rx)}/{reply_len} bytes")
        return rx

    def close(self) -> None:
        self.closed = True

    def _run(self, data: bytes) -> bytes:
        if self.fail_writes:
            raise TransportError("simulated write failure")
        self.streams.append(bytes(data))
        rx = bytearray()
        for cmd in parse(data):
            if cmd.op == p.SET_ADBUS:
                self.adbus = cmd.args[0]
            elif cmd.op == p.SET_ACBUS:
                self.acbus = cmd.args[0]
            elif cmd.op == p.GET_ADBUS:
                value = (self.adbus & p.ADBUS_DIRECTION) | p.DATA_IN
                if self.tube_ready:
                    value |= p.MONX
                if self.enable_readback is not None:
                    value = (value & ~p.HV_EN_BOTH) | self.enable_readback
                rx.append(value)
            elif cmd.op == p.GET_ACBUS:
                value = ((self.acbus & p.ACBUS_DIRECTION) | self.acbus_noise
                         | (self.device_type << 1) & p.DEVICE_TYPE_MASK)
                if self.interlock_closed:
                    value |= p.INTERLOCK
                rx.append(value)
            elif cmd.op in (p.CLOCK_BITS_OUT_TS, p.CLOCK_BYTES_OUT) and self.acbus & p.TSCS:
                if cmd.data[0] == p.TS_CONFIG_WRITE and len(cmd.data) == 2:
                    self.ds1722[0] = cmd.data[1]
                else:
                    self.ds1722_address = cmd.data[0]
            elif cmd.op == p.CLOCK_BYTES_IN:
                count = cmd.read_len
                if self.acbus & p.TSCS:
                    regs = self.ds1722[self.ds1722_address:]
                    rx += bytes(regs[:count])
                elif not self.adbus & p.ADCS and self.read_replies:
                    rx += self.read_replies.popleft()
            elif not cmd.valid:
                if not (self.sync_broken and cmd.op == p.SYNC_PROBE):
                    rx += bytes([p.BAD_COMMAND_ECHO, cmd.op])
        return bytes(rx)

    def commands(self, since: int = 0):
        return [cmd for stream in self.streams[since:] for cmd in parse(stream)]


@pytest.fixture
def fake() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def dev(fake) -> MiniX:
    device = MiniX(fake)
    device.initialize()
    return device
