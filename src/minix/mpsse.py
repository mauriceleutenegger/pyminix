"""Split MPSSE command streams into commands.

Covers the opcodes the device layer sends. Any other byte parses as an
invalid command, which MPSSE answers with 0xFA and the opcode (§5.1).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import protocol as p

KNOWN_OPCODES = frozenset({
    p.SET_ADBUS, p.GET_ADBUS, p.SET_ACBUS, p.GET_ACBUS, p.SET_DIVISOR,
    p.CLOCK_BYTES_OUT, p.CLOCK_BITS_OUT, p.CLOCK_BITS_OUT_TS, p.CLOCK_BYTES_IN,
})

_TWO_ARG_OPCODES = (p.SET_ADBUS, p.SET_ACBUS, p.SET_DIVISOR, p.CLOCK_BYTES_IN)
_BIT_OUT_OPCODES = (p.CLOCK_BITS_OUT, p.CLOCK_BITS_OUT_TS)


@dataclass(frozen=True)
class Command:
    op: int
    args: bytes = b""       # fixed arguments or length field
    data: bytes = b""       # clocked-out payload

    @property
    def valid(self) -> bool:
        return self.op in KNOWN_OPCODES

    @property
    def length(self) -> int:
        """The n of a clocking command's n-1 length field (bytes, or bits for bit mode)."""
        if self.op in (p.CLOCK_BYTES_OUT, p.CLOCK_BYTES_IN):
            return (self.args[0] | self.args[1] << 8) + 1
        if self.op in _BIT_OUT_OPCODES:
            return self.args[0] + 1
        return 0

    @property
    def read_len(self) -> int:
        """Bytes this command puts in the RX buffer."""
        if self.op in (p.GET_ADBUS, p.GET_ACBUS):
            return 1
        if self.op == p.CLOCK_BYTES_IN:
            return self.length
        return 0 if self.valid else 2


def split(stream: bytes) -> tuple[list[Command], bytes]:
    """Split a stream into complete commands and a trailing incomplete one.

    MPSSE waits for the rest of an incomplete command, so the remainder
    belongs in front of the next stream.
    """
    cmds = []
    i = 0
    while i < len(stream):
        op = stream[i]
        start = i
        i += 1
        if op in _TWO_ARG_OPCODES:
            if i + 2 > len(stream):
                return cmds, stream[start:]
            cmds.append(Command(op, stream[i:i + 2]))
            i += 2
        elif op in (p.GET_ADBUS, p.GET_ACBUS):
            cmds.append(Command(op))
        elif op == p.CLOCK_BYTES_OUT:
            if i + 2 > len(stream):
                return cmds, stream[start:]
            n = (stream[i] | stream[i + 1] << 8) + 1
            if i + 2 + n > len(stream):
                return cmds, stream[start:]
            cmds.append(Command(op, stream[i:i + 2], stream[i + 2:i + 2 + n]))
            i += 2 + n
        elif op in _BIT_OUT_OPCODES:
            if i + 2 > len(stream):
                return cmds, stream[start:]
            cmds.append(Command(op, stream[i:i + 1], stream[i + 1:i + 2]))
            i += 2
        else:
            cmds.append(Command(op))
    return cmds, b""


def parse(stream: bytes) -> list[Command]:
    """Split a stream into commands, dropping a trailing incomplete one."""
    return split(stream)[0]
