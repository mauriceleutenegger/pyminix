"""Simulated Mini-X controller.

SimTransport implements the Transport protocol by running the MPSSE
command streams against a model of the board, so the device layer,
controller and GUI can run without hardware. It models what has been
observed or read in the vendor source (docs/protocol.md), not an ideal
part:

* DAC registers; HV enables gated by the interlock; a supply that settles
  exponentially toward the DAC setpoints; MONX asserting after a delay,
  and dropping briefly at random above about 185 µA, as observed near
  200 µA (§10.8). After switch-off the HV falls quickly, then discharges
  slowly over a few seconds.
* ADC replies in the real framing (null bit, trailing B1-B3 repeat), with
  the offsets and noise seen on hardware (§10.2, §10.3, §10.7): more
  noise with the supply on. A reading of 0 is normal with HV off.
* A DS1722 that answers correctly only to the vendor clock sequence
  (§8.3, §8.4), with volatile config (0xE3 at start, as found), conversion
  times, and slow board heating from tube power, fitted to a 39-minute
  run at 8 W (§10.9).
* Random values in ACBUS bits 4-7, which are not pins (§3).

It is stricter than the hardware in one respect: anything the model does
not recognize is recorded in `violations` and ignored rather than
imitated. Tests should assert that list is empty.

`max_commanded_mw` records the highest DAC-setpoint power reached while
the supply was enabled, including intermediate states between two DAC
writes, so tests can check sequencing against the power limit.

Fault injection: `fail_io`, `stuck_enable_readback`, `drop_reply_bytes`,
and `adc_framing_errors` (the next n ADC replies have the null bit set).
Set `interlock_closed` (or call `set_interlock`) from any thread to
simulate the interlock.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, fields

from . import protocol as p
from .mpsse import Command, split
from .transport import TransportError

log = logging.getLogger(__name__)

# Observed on sn 01300036 (§10.2, §10.7, §10.8, docs/hardware-notes.md).
IDLE_OFFSET_COUNTS = 8.6        # both channels, supply off
HV_GAIN = 0.996                 # supply on: 49.79 kV at 50, 19.9 at 20, 14.94 at 15
CURRENT_GAIN = 0.997            # supply on: 198.7 µA at 198.95, 10.3 at 10
CURRENT_ON_OFFSET_COUNTS = 6.0
NOISE_COUNTS = 4.0              # supply off
NOISE_ON_COUNTS = 15.0          # supply on: 14-16 counts measured on 2026-09-17
SETTLE_TAU_S = 0.15             # measured ≈97 % within 0.5 s of a DAC write
# HV after switch-off: 1.1 kV after 1 s, 0.5 kV after 3 s from 15 kV.
DISCHARGE_TAU_S = 0.3
DISCHARGE_TAIL_TAU_S = 2.5
DISCHARGE_TAIL_FRACTION = 0.07
# MONX at high emission current: low on this fraction of reads (§10.8
# measured 7-46 % of 1 Hz samples at 190-200 µA; none at 100 µA).
# Board heating fitted to a 39-minute run at 8 W (§10.9): 17 min, 0.23 °C/W.
THERMAL_TAU_S = 1020.0
MONX_FLICKER_ABOVE_UA = 185.0
MONX_FLICKER_PROBABILITY = 0.2
DS1722_CONFIG_AS_FOUND = 0xE3
DS1722_TEMP_AS_FOUND_C = 25.0

# DS1722 conversion time by resolution, bits -> seconds (datasheet maxima).
DS1722_CONVERSION_S = {8: 0.075, 9: 0.15, 10: 0.3, 11: 0.6, 12: 1.2}


class SimTransport:
    def __init__(
        self,
        serial: str = "01300036",
        *,
        hv_factor: float = p.HV_FACTOR_50KV,
        clock: Callable[[], float] = time.monotonic,
        seed: int | None = 0,
        settle_tau_s: float = SETTLE_TAU_S,
        monx_delay_s: float = 0.3,
        device_type: int = 2,       # MX50.10: 50 kV, 10 W, as on sn 01300036
        ambient_c: float = 27.0,
        heating_c_per_w: float = 0.23,
        noise: bool = True,
    ):
        self._serial = serial
        self._clock = clock
        self._rng = random.Random(seed)
        self.hv_factor = hv_factor
        self.device_type = device_type
        self.settle_tau_s = settle_tau_s
        self.monx_delay_s = monx_delay_s
        self.ambient_c = ambient_c
        self.heating_c_per_w = heating_c_per_w
        self.noise = noise

        # Inputs and fault injection; safe to set from any thread.
        self.interlock_closed = True
        self.fail_io = False
        self.stuck_enable_readback: int | None = None
        self.drop_reply_bytes = 0
        self.adc_framing_errors = 0
        self.monx_flicker_probability = MONX_FLICKER_PROBABILITY

        # Pins as driven by the host. Before MPSSE setup the outputs are off.
        self.adbus = 0x00
        self.acbus = 0x00
        self.divisor = p.CLOCK_DIVISOR[0] | p.CLOCK_DIVISOR[1] << 8

        # Analog state.
        self.hv_dac = 0
        self.current_dac = 0
        self.kv = 0.0
        self.ua = 0.0
        self._kv_tail = 0.0             # slowly discharging part of kv after switch-off
        self._board_c = ambient_c
        self._was_on = False
        self._enabled_since: float | None = None
        self._last_update = clock()

        # DS1722 state.
        self.ds1722_config = DS1722_CONFIG_AS_FOUND
        self._ds1722_temp_reg = _encode_temperature(DS1722_TEMP_AS_FOUND_C, 12)
        self._last_conversion = self._last_update
        self._ts_clock_at_select = 0
        self._ts_address: int | None = None
        self._ts_pointer = 0
        self._ts_write_pending = False

        # ADC state within one chip-select.
        self._adc_channel: int | None = None

        self._pending = b""     # an incomplete command awaiting more bytes
        self._rx = b""          # replies not yet read
        self._closed = False
        self.violations: list[str] = []
        self.max_commanded_mw = 0.0
        self.max_actual_mw = 0.0

    # --- Transport protocol --------------------------------------------------

    @property
    def serial(self) -> str:
        return self._serial

    def write(self, data: bytes) -> None:
        self._rx += self._run(data)

    def exchange(self, data: bytes, reply_len: int) -> bytes:
        self._check_open()
        self._rx = b""   # the purge; the MPSSE parser state survives it
        self._rx = self._run(data)
        if self.drop_reply_bytes:
            self._rx = self._rx[:max(0, len(self._rx) - self.drop_reply_bytes)]
        if len(self._rx) < reply_len:
            got, self._rx = self._rx, b""
            raise TransportError(f"short read: {len(got)}/{reply_len} bytes")
        rx, self._rx = self._rx[:reply_len], self._rx[reply_len:]
        return rx

    def close(self) -> None:
        self._check_open()
        self._closed = True
        # Leaving MPSSE releases the pins; the enables drop.
        self.adbus = 0x00
        self.acbus = 0x00
        self._update_supply()

    # --- inspection and controls ---------------------------------------------

    def set_interlock(self, closed: bool) -> None:
        self.interlock_closed = closed

    @property
    def hv_enabled(self) -> bool:
        """Both enable pins driven high."""
        return self.adbus & p.HV_EN_BOTH == p.HV_EN_BOTH

    @property
    def supply_on(self) -> bool:
        return self.hv_enabled and self.interlock_closed and not self._closed

    @property
    def tube_ready(self) -> bool:
        self._advance()
        return self._monx()

    @property
    def hv_setpoint_kv(self) -> float:
        return p.counts_to_volts(self.hv_dac) * self.hv_factor

    @property
    def current_setpoint_ua(self) -> float:
        return p.counts_to_volts(self.current_dac) * p.CURRENT_FACTOR

    @property
    def board_temperature_c(self) -> float:
        return self._board_c

    def reset_peaks(self) -> None:
        self.max_commanded_mw = 0.0
        self.max_actual_mw = 0.0

    def advance(self) -> None:
        """Bring the analog state up to the current clock time."""
        self._advance()

    # --- stream execution ----------------------------------------------------

    def _run(self, data: bytes) -> bytes:
        self._check_open()
        if self.fail_io:
            raise TransportError("simulated I/O failure")
        self._advance()
        cmds, self._pending = split(self._pending + bytes(data))
        rx = bytearray()
        for cmd in cmds:
            rx += self._execute(cmd)
        return bytes(rx)

    def _execute(self, cmd: Command) -> bytes:
        op = cmd.op
        if op == p.SET_ADBUS:
            self._set_adbus(cmd.args[0], cmd.args[1])
        elif op == p.SET_ACBUS:
            self._set_acbus(cmd.args[0], cmd.args[1])
        elif op == p.SET_DIVISOR:
            self.divisor = cmd.args[0] | cmd.args[1] << 8
        elif op == p.GET_ADBUS:
            return bytes([self._read_adbus()])
        elif op == p.GET_ACBUS:
            return bytes([self._read_acbus()])
        elif op in (p.CLOCK_BYTES_OUT, p.CLOCK_BITS_OUT, p.CLOCK_BITS_OUT_TS, p.CLOCK_BYTES_IN):
            return self._clock_data(cmd)
        else:
            return bytes([p.BAD_COMMAND_ECHO, op])
        return b""

    # --- pins ----------------------------------------------------------------

    def _set_adbus(self, state: int, direction: int) -> None:
        if direction != p.ADBUS_DIRECTION:
            self._violation(f"ADBUS direction {direction:#04x}")
            return
        was_enabled = self.hv_enabled
        old = self.adbus
        self.adbus = state
        if state & p.HV_EN_BOTH not in (0, p.HV_EN_BOTH):
            self._violation(f"only one HV enable bit set (ADBUS {state:08b})")
        if old & p.ADCS == 0 and state & p.ADCS:
            self._adc_channel = None
        if self.hv_enabled and not was_enabled:
            self._enabled_since = self._clock()
        elif was_enabled and not self.hv_enabled:
            self._enabled_since = None
        self._update_supply()

    def _set_acbus(self, state: int, direction: int) -> None:
        if direction != p.ACBUS_DIRECTION:
            self._violation(f"ACBUS direction {direction:#04x}")
            return
        rising = state & p.TSCS and not self.acbus & p.TSCS
        self.acbus = state
        if rising:
            self._ts_clock_at_select = self.adbus & p.CLK
            self._ts_address = None
            self._ts_write_pending = False

    def _read_adbus(self) -> int:
        value = (self.adbus & p.ADBUS_DIRECTION) | p.DATA_IN
        if self._monx():
            value |= p.MONX
        if self.stuck_enable_readback is not None:
            value = (value & ~p.HV_EN_BOTH) | self.stuck_enable_readback
        return value

    def _read_acbus(self) -> int:
        value = (self.acbus & p.ACBUS_DIRECTION) | (self.device_type << 1) & p.DEVICE_TYPE_MASK
        if self.interlock_closed:
            value |= p.INTERLOCK
        if self.noise:
            value |= self._rng.choice((0x00, 0x00, 0x40, 0x80))
        return value

    def _selected(self) -> list[str]:
        chips = []
        if not self.adbus & p.ADCS:
            chips.append("adc")
        if not self.adbus & p.DACS:
            chips.append("dac")
        if self.acbus & p.TSCS:
            chips.append("ds1722")
        return chips

    # --- peripherals ---------------------------------------------------------

    def _clock_data(self, cmd: Command) -> bytes:
        chips = self._selected()
        if len(chips) != 1:
            self._violation(f"clocking {cmd.op:#04x} with {chips or 'nothing'} selected")
            return bytes(cmd.read_len)
        chip = chips[0]
        if chip == "dac":
            return self._dac(cmd)
        if chip == "adc":
            return self._adc(cmd)
        return self._ds1722(cmd)

    def _dac(self, cmd: Command) -> bytes:
        if cmd.op != p.CLOCK_BYTES_OUT or len(cmd.data) != 3:
            self._violation(f"DAC selected for {cmd.op:#04x} with {len(cmd.data)} bytes")
            return bytes(cmd.read_len)
        if not self.adbus & p.CLK:
            self._violation("DAC write with the clock idle low; ignored")
            return b""
        command, bhi, blo = cmd.data
        counts = bhi << 4 | blo >> 4
        if command == p.DAC_HV:
            self.hv_dac = counts
        elif command == p.DAC_CURRENT:
            self.current_dac = counts
        else:
            self._violation(f"DAC command byte {command:#04x}")
        self._update_supply()
        return b""

    def _adc(self, cmd: Command) -> bytes:
        if cmd.op == p.CLOCK_BITS_OUT and cmd.length == p.ADC_NIBBLE_BITS:
            nibble = cmd.data[0] >> 4
            if self.adbus & p.CLK:
                self._violation("ADC nibble with the clock idle high")
            elif nibble not in (p.ADC_HV >> 4, p.ADC_CURRENT >> 4):
                self._violation(f"ADC control nibble {nibble:04b}")
            else:
                self._adc_channel = 1 if nibble & 0x02 else 0
            return b""
        if cmd.op == p.CLOCK_BYTES_IN and cmd.length == 2:
            if self._adc_channel is None:
                self._violation("ADC read without a control nibble")
                return bytes(2)
            counts = self._adc_counts(self._adc_channel)
            raw = counts << 3 | _trailing_bits(counts)
            if self.adc_framing_errors > 0:
                self.adc_framing_errors -= 1
                raw |= 0x8000
            return raw.to_bytes(2, "big")
        self._violation(f"ADC selected for {cmd.op:#04x}")
        return bytes(cmd.read_len)

    def _adc_counts(self, channel: int) -> int:
        on = self.supply_on
        if channel == 0:
            volts = self.kv / self.hv_factor * (HV_GAIN if on else 1.0)
            offset = 0.0 if on else IDLE_OFFSET_COUNTS
        else:
            volts = self.ua / p.CURRENT_FACTOR * (CURRENT_GAIN if on else 1.0)
            offset = CURRENT_ON_OFFSET_COUNTS if on else IDLE_OFFSET_COUNTS
        counts = volts / p.VREF * p.DAC_ADC_SCALE + offset
        if self.noise:
            counts += self._rng.gauss(0.0, NOISE_ON_COUNTS if on else NOISE_COUNTS)
        return min(p.COUNTS_MAX, max(0, round(counts)))

    def _ds1722(self, cmd: Command) -> bytes:
        # The vendor sequence: TSCS rises with the clock low, and the clock
        # is high during the transfer. Anything else misframes (§8.4).
        framed = self._ts_clock_at_select == 0 and self.adbus & p.CLK
        if cmd.op in (p.CLOCK_BYTES_OUT, p.CLOCK_BITS_OUT_TS):
            if cmd.op == p.CLOCK_BITS_OUT_TS and cmd.length != 8:
                self._violation(f"DS1722 bit transfer of {cmd.length} bits")
                return b""
            for byte in cmd.data:
                self._ds1722_byte_in(byte if framed else byte >> 1, framed)
            return b""
        if cmd.op == p.CLOCK_BYTES_IN:
            self._convert_temperature()
            out = bytes(self._ds1722_next() for _ in range(cmd.length))
            if framed:
                return out
            shifted = int.from_bytes(out, "big") >> 1
            return shifted.to_bytes(len(out), "big")
        self._violation(f"DS1722 selected for {cmd.op:#04x}")
        return bytes(cmd.read_len)

    def _ds1722_byte_in(self, byte: int, framed: bool) -> None:
        if self._ts_write_pending:
            self._ts_write_pending = False
            self.ds1722_config = 0xE0 | (byte & 0x1F)
            self._last_conversion = self._clock()
            return
        if self._ts_address is not None:
            # A misframed write lands here as a bogus address plus data.
            if framed:
                self._violation("DS1722 received a second byte after a read address")
            return
        self._ts_address = byte
        if byte == p.TS_CONFIG_WRITE:
            self._ts_write_pending = True
        else:
            self._ts_pointer = byte

    def _ds1722_next(self) -> int:
        regs = {
            p.TS_CONFIG_READ: self.ds1722_config,
            p.TS_TEMP_LSB: self._ds1722_temp_reg & 0xFF,
            p.TS_TEMP_MSB: self._ds1722_temp_reg >> 8,
        }
        value = regs.get(self._ts_pointer, 0x00)
        self._ts_pointer += 1
        return value

    def _convert_temperature(self) -> None:
        config = self.ds1722_config
        if config & 0x01 and not config & 0x10:
            return  # shut down, no one-shot pending
        bits = _resolution_bits(config)
        now = self._clock()
        if now - self._last_conversion >= DS1722_CONVERSION_S[bits]:
            self._ds1722_temp_reg = _encode_temperature(self.board_temperature_c, bits)
            self._last_conversion = now
            self.ds1722_config &= ~0x10   # a one-shot completes

    # --- supply dynamics -----------------------------------------------------

    def _monx(self) -> bool:
        ready = (self.supply_on and self._enabled_since is not None
                 and self._clock() - self._enabled_since >= self.monx_delay_s)
        if (ready and self.ua > MONX_FLICKER_ABOVE_UA
                and self._rng.random() < self.monx_flicker_probability):
            return False
        return ready

    def _advance(self) -> None:
        now = self._clock()
        dt = max(0.0, now - self._last_update)
        self._last_update = now
        if dt == 0.0:
            return
        on = self.supply_on
        if on != self._was_on:
            self._was_on = on
            self._kv_tail = 0.0 if on else self.kv * DISCHARGE_TAIL_FRACTION
        k = 1.0 - math.exp(-dt / self.settle_tau_s) if self.settle_tau_s > 0 else 1.0
        if on:
            self.kv += (self.hv_setpoint_kv - self.kv) * k
            self.ua += (self.current_setpoint_ua - self.ua) * k
        else:
            fast = (self.kv - self._kv_tail) * math.exp(-dt / DISCHARGE_TAU_S)
            self._kv_tail *= math.exp(-dt / DISCHARGE_TAIL_TAU_S)
            self.kv = fast + self._kv_tail
            self.ua -= self.ua * k
        self.max_actual_mw = max(self.max_actual_mw, self.kv * self.ua)
        target_c = self.ambient_c + self.heating_c_per_w * self.kv * self.ua / 1000.0
        self._board_c += (target_c - self._board_c) * (1.0 - math.exp(-dt / THERMAL_TAU_S))

    def _update_supply(self) -> None:
        if self.supply_on:
            commanded = self.hv_setpoint_kv * self.current_setpoint_ua
            self.max_commanded_mw = max(self.max_commanded_mw, commanded)

    # --- plumbing ------------------------------------------------------------

    def _violation(self, message: str) -> None:
        log.warning("sim protocol violation: %s", message)
        self.violations.append(message)

    def _check_open(self) -> None:
        if self._closed:
            raise TransportError("simulated device is closed")


@dataclass
class SimFaults:
    """Operator-controlled conditions for the simulator panel."""

    interlock_closed: bool = True
    tube_never_ready: bool = False
    supply_stuck: bool = False
    enables_stuck_on: bool = False
    usb_failure: bool = False
    monx_flicker: bool = True

    LABELS = {
        "interlock_closed": "Interlock closed",
        "tube_never_ready": "Tube never ready",
        "supply_stuck": "Supply stuck at 0",
        "enables_stuck_on": "Enable readback stuck on",
        "usb_failure": "USB failure",
        "monx_flicker": "MONX flicker near 200 µA",
    }

    @classmethod
    def names(cls) -> list[str]:
        return [f.name for f in fields(cls)]


class SimHarness:
    """Creates simulated controllers with the current SimFaults applied.

    set_fault() applies a change to the current simulator immediately and
    to every one created later, so the panel and the simulator never
    disagree across reconnects. create() runs on the controller's worker
    thread, set_fault() on the GUI thread; both only assign attributes.
    """

    def __init__(self, **sim_options):
        self._options = sim_options
        defaults = SimTransport(**sim_options)
        self._normal_monx_delay_s = defaults.monx_delay_s
        self._normal_settle_tau_s = defaults.settle_tau_s
        self.faults = SimFaults()
        self.current: SimTransport | None = None

    def create(self, serial: str) -> SimTransport:
        sim = SimTransport(serial, **self._options)
        self._apply(sim)
        self.current = sim
        return sim

    def set_fault(self, name: str, value: bool) -> None:
        if name not in SimFaults.names():
            raise AttributeError(f"unknown simulator fault {name!r}")
        setattr(self.faults, name, value)
        if self.current is not None:
            self._apply(self.current)

    def _apply(self, sim: SimTransport) -> None:
        f = self.faults
        sim.interlock_closed = f.interlock_closed
        sim.monx_delay_s = 1e9 if f.tube_never_ready else self._normal_monx_delay_s
        sim.settle_tau_s = 1e9 if f.supply_stuck else self._normal_settle_tau_s
        sim.stuck_enable_readback = p.HV_EN_BOTH if f.enables_stuck_on else None
        sim.fail_io = f.usb_failure
        sim.monx_flicker_probability = MONX_FLICKER_PROBABILITY if f.monx_flicker else 0.0


def _trailing_bits(counts: int) -> int:
    return (counts >> 1 & 1) << 2 | (counts >> 2 & 1) << 1 | (counts >> 3 & 1)


def _resolution_bits(config: int) -> int:
    code = config >> 1 & 0x07
    return 12 if code & 0x04 else 8 + code


def _encode_temperature(celsius: float, bits: int) -> int:
    """DS1722 register pair (MSB << 8 | LSB) at the given resolution."""
    step = 2.0 ** (8 - bits)                    # °C per LSB
    celsius = min(p.TS_MAX_C, max(p.TS_MIN_C, celsius))
    sixteenths = int(math.floor(celsius / step) * step * 16)
    raw12 = sixteenths & 0x0FFF
    return raw12 << 4
