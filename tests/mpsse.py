"""Parse MPSSE command streams, for asserting on what the device layer sends.

Covers the opcodes this project uses. Anything else parses as an invalid
command, which MPSSE answers with 0xFA and the opcode.
"""

from __future__ import annotations

from dataclasses import dataclass

from minix import protocol as p

KNOWN_OPCODES = {
    p.SET_ADBUS, p.GET_ADBUS, p.SET_ACBUS, p.GET_ACBUS, p.SET_DIVISOR,
    p.CLOCK_BYTES_OUT, p.CLOCK_BITS_OUT, p.CLOCK_BITS_OUT_TS, p.CLOCK_BYTES_IN,
}


@dataclass(frozen=True)
class Command:
    op: int
    args: bytes = b""       # fixed arguments or length field
    data: bytes = b""       # clocked-out payload

    @property
    def valid(self) -> bool:
        return self.op in KNOWN_OPCODES

    @property
    def read_len(self) -> int:
        """Bytes this command puts in the RX buffer."""
        if self.op in (p.GET_ADBUS, p.GET_ACBUS):
            return 1
        if self.op == p.CLOCK_BYTES_IN:
            return (self.args[0] | self.args[1] << 8) + 1
        return 0 if self.valid else 2


def parse(stream: bytes) -> list[Command]:
    """Split a stream into commands. A truncated final command is dropped."""
    cmds = []
    i = 0
    while i < len(stream):
        op = stream[i]
        i += 1
        if op in (p.SET_ADBUS, p.SET_ACBUS, p.SET_DIVISOR, p.CLOCK_BYTES_IN):
            if i + 2 > len(stream):
                break
            cmds.append(Command(op, stream[i:i + 2]))
            i += 2
        elif op in (p.GET_ADBUS, p.GET_ACBUS):
            cmds.append(Command(op))
        elif op == p.CLOCK_BYTES_OUT:
            if i + 2 > len(stream):
                break
            n = (stream[i] | stream[i + 1] << 8) + 1
            if i + 2 + n > len(stream):
                break
            cmds.append(Command(op, stream[i:i + 2], stream[i + 2:i + 2 + n]))
            i += 2 + n
        elif op in (p.CLOCK_BITS_OUT, p.CLOCK_BITS_OUT_TS):
            if i + 2 > len(stream):
                break
            cmds.append(Command(op, stream[i:i + 1], stream[i + 1:i + 2]))
            i += 2
        else:
            cmds.append(Command(op))
    return cmds
