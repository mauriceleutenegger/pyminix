"""Peripheral transactions.

MiniX drives the DAC (§6.2), ADC (§6.3), GPIO (§7) and DS1722 (§8) over a
Transport. It knows MPSSE framing; it knows nothing about setpoint limits,
power ratings or sequencing, which belong to policy and the controller.
It works in DAC/ADC counts; engineering units depend on the unit config.

Rules this module enforces:

* The HV enable bits change only in set_hv_enable() and failsafe(). Every
  other transaction rewrites the ADBUS state byte but carries the enable
  bits through unchanged.
* set_hv_enable(True) refuses when the interlock reads open, and both
  directions verify the enable bits by reading them back.
* Every read returns a value or raises. A transport failure mid-transaction
  leaves the pin state unknown, so the device must be re-initialized before
  further use; failsafe() still works.
* A MiniX belongs to the thread that created it. Two threads interleaving
  MPSSE streams could turn one command's bytes into another's DAC data
  (§11), so calls from any other thread raise RuntimeError.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from . import protocol as p
from .transport import Transport, TransportError

log = logging.getLogger(__name__)

DAC_CHANNELS = (p.DAC_HV, p.DAC_CURRENT)
ADC_CHANNELS = (p.ADC_HV, p.ADC_CURRENT)

# Full-speed USB bulk packets carry 64 bytes. A stream no longer than that
# is delivered whole or not at all, so the MPSSE parser never sees a
# truncated command. Keep every write within this limit.
MAX_STREAM_BYTES = 64


class DeviceError(Exception):
    """The controller responded, but not as the protocol requires."""


class NotInitializedError(DeviceError):
    """initialize() has not run, or a transport failure invalidated it."""


class SyncError(DeviceError):
    """The MPSSE sync check did not get its echo back (§5.1)."""


class FramingError(DeviceError):
    """An ADC reply is misaligned: null bit set, or trailing bits wrong (§6.3)."""


class InterlockOpenError(DeviceError):
    """HV enable refused because the interlock reads open (§7.3)."""


class HvEnableError(DeviceError):
    """The enable bits read back differently from what was written (§9.2)."""

    def __init__(self, message: str, state: GpioState):
        super().__init__(message)
        self.state = state


class TemperatureError(DeviceError):
    """A DS1722 reply that cannot be a real temperature."""


@dataclass(frozen=True)
class GpioState:
    adbus: int
    acbus: int

    @property
    def interlock_closed(self) -> bool:
        return bool(self.acbus & p.INTERLOCK)

    @property
    def hv_enabled(self) -> bool:
        """Both enable bits set."""
        return self.adbus & p.HV_EN_BOTH == p.HV_EN_BOTH

    @property
    def hv_disabled(self) -> bool:
        """Both enable bits clear."""
        return not self.adbus & p.HV_EN_BOTH

    @property
    def hv_partial(self) -> bool:
        """Exactly one enable bit set: always a fault (§7.2)."""
        return not (self.hv_enabled or self.hv_disabled)

    @property
    def tube_ready(self) -> bool:
        """MONX: the supply is up (§7.2)."""
        return bool(self.adbus & p.MONX)

    @property
    def chip_selects_idle(self) -> bool:
        """Both active-low chip selects deasserted, as they should be between transactions."""
        return self.adbus & (p.ADCS | p.DACS) == p.ADCS | p.DACS


@dataclass(frozen=True)
class AdcReading:
    counts: int
    raw: bytes

    @property
    def volts(self) -> float:
        return p.counts_to_volts(self.counts)



def unpack_adc(rx: bytes) -> int:
    """1 null bit + 12 data bits + 3 trailing bits (§6.3).

    The trailing bits are not padding: the converter continues LSB-first
    after the LSB, so they repeat B1, B2, B3 (0 mismatches in 412 hardware
    readings). Checking them catches shifted frames that the null bit
    misses. An all-zero reply passes both checks, so neither can detect a
    dead data line.
    """
    raw = ((rx[0] << 8) | rx[1]) >> 3
    if raw & p.ADC_NULL_BIT:
        raise FramingError(f"ADC null bit set: reply {rx.hex()}")
    counts = raw & p.ADC_DATA_MASK
    if rx[1] & 0x07 != adc_trailing_bits(counts):
        raise FramingError(f"ADC trailing bits do not repeat B1-B3: reply {rx.hex()}")
    return counts


def adc_trailing_bits(counts: int) -> int:
    """B1, B2, B3 of counts, in the order the converter repeats them."""
    return (counts >> 1 & 1) << 2 | (counts >> 2 & 1) << 1 | (counts >> 3 & 1)


def decode_temperature_c(msb: int, lsb: int) -> float:
    """DS1722 12-bit reading, left-justified across two bytes (§8.2)."""
    if lsb & 0x0F:
        raise TemperatureError(
            f"DS1722 reply MSB {msb:#04x} LSB {lsb:#04x}: LSB low bits set, "
            "not a temperature reading")
    raw = (msb << 4) | (lsb >> 4)
    if msb & 0x80:
        raw -= 4096
    celsius = raw * p.TS_LSB_C
    if not p.TS_MIN_C <= celsius <= p.TS_MAX_C:
        raise TemperatureError(
            f"DS1722 reply MSB {msb:#04x} LSB {lsb:#04x}: {celsius} °C is outside its range")
    return celsius


class MiniX:
    def __init__(self, transport: Transport):
        self._transport = transport
        self._owner = threading.get_ident()
        # Shadows of the output pin states: MPSSE sets whole bytes, so each
        # write must carry every bit.
        self._adbus = p.ADBUS_INIT
        self._acbus = p.ACBUS_INIT
        self._initialized = False

    @property
    def serial(self) -> str:
        return self._transport.serial

    @property
    def initialized(self) -> bool:
        return self._initialized

    # --- setup ---------------------------------------------------------------

    def initialize(self) -> None:
        """Set pin directions and idle states with HV disabled, set the clock
        divisor, and verify MPSSE sync (§5)."""
        self._check_thread()
        self._initialized = False
        self._adbus = p.ADBUS_INIT
        self._acbus = p.ACBUS_INIT
        self._write(self._set_acbus() + self._set_adbus() + self._set_divisor())
        self.check_sync()
        self._initialized = True

    def check_sync(self) -> None:
        """Send an invalid opcode and expect MPSSE to echo it (§5.1)."""
        self._check_thread()
        expected = bytes([p.BAD_COMMAND_ECHO, p.SYNC_PROBE])
        rx = self._exchange(bytes([p.SYNC_PROBE]), 2)
        if rx != expected:
            self._initialized = False
            raise SyncError(f"MPSSE sync check: expected {expected.hex(' ')}, got {rx.hex(' ')}")

    # --- DAC -----------------------------------------------------------------

    def write_dac(self, channel: int, counts: int) -> None:
        """Write and update one DAC channel (§6.2)."""
        self._check_ready()
        if channel not in DAC_CHANNELS:
            raise ValueError(f"not a DAC channel: {channel:#04x}")
        if isinstance(counts, bool) or not isinstance(counts, int):
            raise TypeError(f"DAC counts must be int, got {type(counts).__name__}")
        if not 0 <= counts <= p.COUNTS_MAX:
            raise ValueError(f"DAC counts {counts} outside 0..{p.COUNTS_MAX}")
        self._write(self._dac_stream(channel, counts))

    def _dac_stream(self, channel: int, counts: int) -> bytes:
        bhi = (counts & 0x0FF0) >> 4
        blo = (counts & 0x000F) << 4
        # CLK goes high as DACS asserts: the reverse of the ADC path, and
        # determined against hardware. Do not normalize the two.
        self._adbus = (self._adbus & ~p.DACS) | p.CLK
        tx = self._set_adbus()
        tx += bytes([p.CLOCK_BYTES_OUT, 0x02, 0x00, channel, bhi, blo])
        self._adbus |= p.DACS
        tx += self._set_adbus()
        return tx

    # --- ADC -----------------------------------------------------------------

    def read_adc(self, channel: int) -> AdcReading:
        """Convert one ADC channel (§6.3)."""
        self._check_ready()
        if channel not in ADC_CHANNELS:
            raise ValueError(f"not an ADC channel: {channel:#04x}")
        tx = self._set_divisor()
        # CLK goes low as ADCS asserts: see _dac_stream.
        self._adbus &= ~(p.ADCS | p.CLK)
        tx += self._set_adbus()
        tx += bytes([p.CLOCK_BITS_OUT, p.ADC_NIBBLE_BITS - 1, channel])
        # "INPUTMODE" write: a no-op on rev C0 (same direction byte), kept
        # because it turned the data line around on earlier boards.
        tx += self._set_adbus()
        tx += bytes([p.CLOCK_BYTES_IN, 0x01, 0x00])
        self._adbus |= p.ADCS
        tx += self._set_adbus()
        rx = self._exchange(tx, 2)
        return AdcReading(counts=unpack_adc(rx), raw=rx)

    # --- GPIO and HV enable --------------------------------------------------

    def read_gpio(self) -> GpioState:
        """Read both pin bytes (§7.1). Outputs read back their driven state."""
        self._check_ready()
        return self._read_gpio()

    def _read_gpio(self) -> GpioState:
        return self._gpio_state(self._exchange(bytes([p.GET_ADBUS, p.GET_ACBUS]), 2))

    @staticmethod
    def _gpio_state(rx: bytes) -> GpioState:
        return GpioState(adbus=rx[0], acbus=rx[1] & p.ACBUS_PINS)

    def set_hv_enable(self, on: bool) -> GpioState:
        """Set or clear both HV enable bits and verify them by readback.

        Enabling requires a closed interlock. Disabling is always attempted.
        This method does not touch the DACs; their state around enabling is
        the controller's responsibility (§9.2).
        """
        if on:
            self._check_ready()
            before = self._read_gpio()
            if not before.interlock_closed:
                raise InterlockOpenError("interlock is open; HV enable refused")
            self._adbus |= p.HV_EN_BOTH
        else:
            self._check_thread()
            self._adbus &= ~p.HV_EN_BOTH
        state = self._gpio_state(
            self._exchange(self._set_adbus() + bytes([p.GET_ADBUS, p.GET_ACBUS]), 2))
        if on and not state.hv_enabled:
            cleared = self.failsafe()
            outcome = ("failsafe confirmed enables clear" if cleared
                       else "failsafe could NOT confirm enables clear; check the tube physically")
            raise HvEnableError(
                f"HV enable did not read back (ADBUS {state.adbus:08b}); {outcome}", state)
        if not on and not state.hv_disabled:
            raise HvEnableError(
                f"HV enable bits still set after disable (ADBUS {state.adbus:08b}); "
                "check the tube physically", state)
        log.info("HV enable %s confirmed", "on" if on else "off")
        return state

    def failsafe(self) -> bool:
        """Clear both HV enables, then zero both DACs. Best effort.

        Never raises transport or device errors; logs them instead. Returns
        True only when a readback confirms both enable bits clear.

        The enables are cleared first because that is the fastest way to
        stop HV. The normal de-energize order (DACs first, §9.2) is the
        controller's job.
        """
        self._check_thread()
        ok = True
        self._adbus = p.ADBUS_INIT  # enables clear, chip selects deasserted
        # No MPSSE resync is attempted: every stream fits in one USB packet
        # (MAX_STREAM_BYTES), so a failed write cannot leave half a command
        # in the parser.
        try:
            self._transport.write(self._set_adbus())
        except TransportError as exc:
            ok = False
            log.critical("failsafe: enable clear write failed: %s", exc)
        for channel in DAC_CHANNELS:
            try:
                self._transport.write(self._dac_stream(channel, 0))
            except TransportError as exc:
                ok = False
                log.error("failsafe: zeroing DAC %#04x failed: %s", channel, exc)
        try:
            state = self._read_gpio()
        except (TransportError, DeviceError) as exc:
            log.critical("failsafe: enable readback failed: %s; check the tube physically", exc)
            self._initialized = False
            return False
        if not state.hv_disabled:
            log.critical("failsafe: enable bits still set (ADBUS %s); check the tube physically",
                         f"{state.adbus:08b}")
            return False
        if not ok:
            self._initialized = False
        return True

    # --- DS1722 (§8) --------------------------------------------------------
    # The vendor transactions (§8.3), confirmed on hardware 2026-09-17.

    def configure_temperature_sensor(self) -> None:
        """Select continuous 12-bit conversion (§8.1).

        The sensor's configuration is volatile, so call this after every
        power-up. Unlike the vendor, the data line is held low at select, as
        in the hardware probe.
        """
        self._check_ready()
        tx = self._set_divisor()
        tx += self._ts_select()
        tx += bytes([p.CLOCK_BYTES_OUT, 0x01, 0x00,
                     p.TS_CONFIG_WRITE, p.TS_CONFIG_12BIT_CONTINUOUS])
        tx += self._ts_deselect()
        self._write(tx)

    def read_temperature_c(self) -> float:
        """Read the board temperature in °C (§8.2).

        Reads the config register along with the temperature and requires it
        to show continuous 12-bit conversion. That checks the frame (a
        shifted frame can still decode to a plausible temperature) and
        refuses a stale reading from a sensor that is shut down.
        """
        self._check_ready()
        tx = self._set_divisor()
        tx += self._ts_select()
        tx += bytes([p.CLOCK_BITS_OUT_TS, 0x07, p.TS_CONFIG_READ])
        tx += self._set_adbus()  # the vendor's "INPUTMODE" rewrite
        tx += bytes([p.CLOCK_BYTES_IN, 0x02, 0x00])  # config, LSB, MSB
        tx += self._ts_deselect()
        config, lsb, msb = self._exchange(tx, 3)
        if config & p.TS_CONFIG_FIXED != p.TS_CONFIG_FIXED:
            raise TemperatureError(
                f"DS1722 config byte {config:#04x} lacks its fixed bits; frame misaligned")
        if config != p.TS_CONFIG_12BIT_CONTINUOUS:
            raise TemperatureError(
                f"DS1722 config is {config:#04x}, not {p.TS_CONFIG_12BIT_CONTINUOUS:#04x}; "
                "the sensor is not configured and its reading may be stale")
        return decode_temperature_c(msb=msb, lsb=lsb)

    def _ts_select(self) -> bytes:
        # TSCS (active high) rises with clock and data low; the low state is
        # written twice more, then the clock goes high for the transfer. The
        # board inverts the clock. Both steps are needed (§8.3, §8.4).
        self._adbus &= ~(p.CLK | p.DATA_OUT)
        self._acbus |= p.TSCS
        tx = self._set_adbus() + self._set_acbus() + self._set_adbus() * 2
        self._adbus |= p.CLK
        return tx + self._set_adbus()

    def _ts_deselect(self) -> bytes:
        self._acbus &= ~p.TSCS
        return self._set_acbus()

    # --- teardown ------------------------------------------------------------

    def close(self) -> bool:
        """Run failsafe(), then close the transport.

        Returns failsafe()'s result: True only if the enables were confirmed clear.
        """
        confirmed = self.failsafe()
        self._initialized = False
        self._transport.close()
        return confirmed

    # --- plumbing ------------------------------------------------------------

    def _set_adbus(self) -> bytes:
        return bytes([p.SET_ADBUS, self._adbus, p.ADBUS_DIRECTION])

    def _set_acbus(self) -> bytes:
        return bytes([p.SET_ACBUS, self._acbus, p.ACBUS_DIRECTION])

    @staticmethod
    def _set_divisor() -> bytes:
        return bytes([p.SET_DIVISOR, *p.CLOCK_DIVISOR])

    def _write(self, data: bytes) -> None:
        try:
            self._transport.write(data)
        except TransportError:
            self._initialized = False
            raise

    def _exchange(self, data: bytes, reply_len: int) -> bytes:
        try:
            return self._transport.exchange(data, reply_len)
        except TransportError:
            self._initialized = False
            raise

    def _check_thread(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError("MiniX used from a thread other than the one that created it")

    def _check_ready(self) -> None:
        self._check_thread()
        if not self._initialized:
            raise NotInitializedError("MiniX is not initialized; call initialize()")
