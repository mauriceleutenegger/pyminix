import threading

import pytest

from minix import protocol as p
from minix.device import (
    ADC_CHANNELS, DAC_CHANNELS, MAX_STREAM_BYTES, FramingError, GpioState, HvEnableError,
    InterlockOpenError, MiniX, NotInitializedError, SyncError, TemperatureError,
    decode_temperature_c,
)
from minix.transport import TransportError
from minix.mpsse import KNOWN_OPCODES

ADC_REPLY = bytes([0x25, 0xA2])   # 1204 counts, from §10.1


def set_adbus_states(cmds):
    return [c.args[0] for c in cmds if c.op == p.SET_ADBUS]


# --- initialization ----------------------------------------------------------

def test_initialize_stream(fake):
    dev = MiniX(fake)
    dev.initialize()
    assert fake.streams == [
        bytes([0x82, 0x00, 0x08, 0x80, 0x9B, 0x7B, 0x86, 0x03, 0x00]),
        bytes([0xAB]),
    ]
    assert dev.initialized


def test_initialize_fails_on_bad_sync(fake):
    fake.sync_broken = True
    dev = MiniX(fake)
    with pytest.raises(TransportError):   # no echo at all: short read
        dev.initialize()
    assert not dev.initialized


def test_sync_rejects_wrong_echo(fake, dev):
    fake.exchange = lambda data, n: b"\xfa\x00"
    with pytest.raises(SyncError):
        dev.check_sync()
    assert not dev.initialized


@pytest.mark.parametrize("call", [
    lambda d: d.write_dac(p.DAC_HV, 0),
    lambda d: d.read_adc(p.ADC_HV),
    lambda d: d.read_gpio(),
    lambda d: d.set_hv_enable(True),
    lambda d: d.read_temperature_c(),
    lambda d: d.configure_temperature_sensor(),
])
def test_operations_require_initialize(fake, call):
    dev = MiniX(fake)
    with pytest.raises(NotInitializedError):
        call(dev)
    assert fake.streams == []


def test_disable_and_failsafe_work_before_initialize(fake):
    dev = MiniX(fake)
    assert dev.set_hv_enable(False).hv_disabled
    assert dev.failsafe()


# --- DAC ---------------------------------------------------------------------

def test_dac_stream(fake, dev):
    start = len(fake.streams)
    dev.write_dac(p.DAC_HV, 1200)
    assert fake.streams[start:] == [bytes([
        0x80, 0x8B, 0x7B,                    # DACS low, CLK high
        0x10, 0x02, 0x00, 0x18, 0x4B, 0x00,  # 1200 = 0x4B0, left-justified
        0x80, 0x9B, 0x7B,                    # DACS high
    ])]


@pytest.mark.parametrize("counts, bhi, blo", [
    (0, 0x00, 0x00), (1, 0x00, 0x10), (200, 0x0C, 0x80), (0xFFF, 0xFF, 0xF0),
])
def test_dac_encoding(fake, dev, counts, bhi, blo):
    start = len(fake.streams)
    dev.write_dac(p.DAC_CURRENT, counts)
    (cmd,) = [c for c in fake.commands(start) if c.op == p.CLOCK_BYTES_OUT]
    assert cmd.data == bytes([p.DAC_CURRENT, bhi, blo])


@pytest.mark.parametrize("channel, counts, exc", [
    (0x1A, 0, ValueError),
    (p.ADC_HV, 0, ValueError),
    (p.DAC_HV, -1, ValueError),
    (p.DAC_HV, 4096, ValueError),
    (p.DAC_HV, 1.0, TypeError),
    (p.DAC_HV, True, TypeError),
])
def test_dac_rejects_bad_arguments(fake, dev, channel, counts, exc):
    start = len(fake.streams)
    with pytest.raises(exc):
        dev.write_dac(channel, counts)
    assert len(fake.streams) == start


# --- ADC ---------------------------------------------------------------------

def test_adc_stream(fake, dev):
    fake.read_replies.append(ADC_REPLY)
    start = len(fake.streams)
    dev.read_adc(p.ADC_CURRENT)
    assert fake.streams[start:] == [bytes([
        0x86, 0x03, 0x00,
        0x80, 0x92, 0x7B,        # ADCS low, CLK low
        0x13, 0x03, 0xF0,        # 4-bit nibble
        0x80, 0x92, 0x7B,        # "INPUTMODE"
        0x20, 0x01, 0x00,        # 2 bytes in
        0x80, 0x9A, 0x7B,        # ADCS high
    ])]


@pytest.mark.parametrize("reply, counts", [
    (bytes([0x25, 0xA2]), 1204),   # §10.1 HV channel
    (bytes([0x06, 0x80]), 208),    # §10.1 current channel
    (bytes([0x00, 0x00]), 0),
    (bytes([0x7F, 0xFF]), 0xFFF),
])
def test_adc_unpack(fake, dev, reply, counts):
    fake.read_replies.append(reply)
    reading = dev.read_adc(p.ADC_HV)
    assert reading.counts == counts
    assert reading.raw == reply
    assert reading.volts == pytest.approx(counts / 4096 * 4.096)


# Every reading from the 2026-09-17 read-only run, plus the two from §10.1.
OBSERVED_ADC_REPLIES = ["0094", "0049", "00a2", "0000", "005d", "0088",
                        "006b", "0036", "001c", "0022", "25a2", "0680"]


@pytest.mark.parametrize("raw", OBSERVED_ADC_REPLIES)
def test_observed_replies_are_accepted(fake, dev, raw):
    fake.read_replies.append(bytes.fromhex(raw))
    dev.read_adc(p.ADC_HV)


# 1204 (25a2) with corrupted trailing bits, and shifted by one bit each way
@pytest.mark.parametrize("raw", ["25a3", "25a0", "4b44", "12d1"])
def test_trailing_bits_catch_bad_frames(fake, dev, raw):
    fake.read_replies.append(bytes.fromhex(raw))
    with pytest.raises(FramingError, match="trailing"):
        dev.read_adc(p.ADC_HV)


def test_adc_framing_error(fake, dev):
    fake.read_replies.append(bytes([0x80, 0x00]))
    with pytest.raises(FramingError, match="null bit"):
        dev.read_adc(p.ADC_HV)


def test_adc_short_read_invalidates(fake, dev):
    with pytest.raises(TransportError):
        dev.read_adc(p.ADC_HV)        # no reply queued
    assert not dev.initialized
    with pytest.raises(NotInitializedError):
        dev.read_adc(p.ADC_HV)


def test_adc_rejects_bad_channel(dev):
    with pytest.raises(ValueError):
        dev.read_adc(p.DAC_HV)


# --- GPIO and HV enable ------------------------------------------------------

def test_gpio_idle_matches_hardware_observation(dev):
    state = dev.read_gpio()
    # §7.1, HV off and idle: ADBUS 0001 1111, ACBUS 0000 0101
    assert state == GpioState(adbus=0x1F, acbus=0x05)
    assert state.interlock_closed
    assert state.hv_disabled and not state.hv_enabled and not state.hv_partial
    assert not state.tube_ready
    assert state.chip_selects_idle


def test_gpio_masks_nonexistent_acbus_bits(fake, dev):
    fake.acbus_noise = 0xC0                     # observed 0x45 and 0x85
    assert dev.read_gpio().acbus == 0x05
    assert dev.set_hv_enable(True).acbus == 0x05


def test_gpio_state_decoding():
    on = GpioState(adbus=0xFF, acbus=0x05)          # §7.1, HV on
    assert on.hv_enabled and on.tube_ready and on.interlock_closed
    assert GpioState(adbus=0x3F, acbus=0x05).hv_partial
    assert GpioState(adbus=0x5F, acbus=0x05).hv_partial
    assert not GpioState(adbus=0x1F, acbus=0x04).interlock_closed
    assert not GpioState(adbus=0x17, acbus=0x05).chip_selects_idle


def test_enable_and_disable(fake, dev):
    fake.tube_ready = True
    state = dev.set_hv_enable(True)
    assert state.hv_enabled and state.tube_ready
    assert fake.adbus & p.HV_EN_BOTH == p.HV_EN_BOTH
    assert dev.set_hv_enable(False).hv_disabled
    assert not fake.adbus & p.HV_EN_BOTH


def test_enable_refused_when_interlock_open(fake, dev):
    fake.interlock_closed = False
    start = len(fake.streams)
    with pytest.raises(InterlockOpenError):
        dev.set_hv_enable(True)
    assert all(not s & p.HV_EN_BOTH for s in set_adbus_states(fake.commands(start)))
    # and the refused attempt must not leak into later transactions
    dev.write_dac(p.DAC_HV, 0)
    assert all(not s & p.HV_EN_BOTH for s in set_adbus_states(fake.commands(start)))


@pytest.mark.parametrize("readback", [0x00, p.HV_EN_A, p.HV_EN_B])
def test_enable_readback_mismatch_triggers_failsafe(fake, dev, readback):
    fake.enable_readback = readback
    with pytest.raises(HvEnableError) as info:
        dev.set_hv_enable(True)
    assert info.value.state.adbus & p.HV_EN_BOTH == readback
    assert not fake.adbus & p.HV_EN_BOTH          # failsafe cleared the pins


def test_disable_readback_still_set(fake, dev):
    dev.set_hv_enable(True)
    fake.enable_readback = p.HV_EN_BOTH
    with pytest.raises(HvEnableError, match="check the tube physically"):
        dev.set_hv_enable(False)


# --- conformance: what every transaction must preserve ------------------------

TRANSACTIONS = {
    "dac_hv": lambda d: d.write_dac(p.DAC_HV, 1200),
    "dac_current": lambda d: d.write_dac(p.DAC_CURRENT, 200),
    "adc_hv": lambda d: d.read_adc(p.ADC_HV),
    "adc_current": lambda d: d.read_adc(p.ADC_CURRENT),
    "gpio": lambda d: d.read_gpio(),
    "temp_config": lambda d: d.configure_temperature_sensor(),
    "temp_read": lambda d: d.read_temperature_c(),
    "sync": lambda d: d.check_sync(),
}


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("name", TRANSACTIONS)
def test_transactions_never_change_enable_bits(fake, dev, name, enabled):
    if enabled:
        dev.set_hv_enable(True)
    fake.read_replies.append(ADC_REPLY)
    start = len(fake.streams)
    TRANSACTIONS[name](dev)
    expected = p.HV_EN_BOTH if enabled else 0
    for state in set_adbus_states(fake.commands(start)):
        assert state & p.HV_EN_BOTH == expected
    assert fake.adbus & p.HV_EN_BOTH == expected


@pytest.mark.parametrize("name", TRANSACTIONS)
def test_transactions_end_with_all_chip_selects_idle(fake, dev, name):
    fake.read_replies.append(ADC_REPLY)
    TRANSACTIONS[name](dev)
    assert fake.adbus & (p.ADCS | p.DACS) == p.ADCS | p.DACS
    assert not fake.acbus & p.TSCS


@pytest.mark.parametrize("name", TRANSACTIONS)
def test_one_peripheral_selected_at_a_time(fake, dev, name):
    fake.read_replies.append(ADC_REPLY)
    start = len(fake.streams)
    TRANSACTIONS[name](dev)
    adbus, acbus = fake.adbus, fake.acbus
    for cmd in fake.commands(start):
        if cmd.op == p.SET_ADBUS:
            adbus = cmd.args[0]
        elif cmd.op == p.SET_ACBUS:
            acbus = cmd.args[0]
        selected = [not adbus & p.ADCS, not adbus & p.DACS, bool(acbus & p.TSCS)]
        assert sum(selected) <= 1


def test_only_known_opcodes_are_sent(fake, dev):
    fake.read_replies.extend([ADC_REPLY] * 2)
    for name, call in TRANSACTIONS.items():
        call(dev)
    dev.set_hv_enable(True)
    dev.set_hv_enable(False)
    dev.failsafe()
    ops = {cmd.op for cmd in fake.commands()}
    # the sync probe is the one deliberate invalid opcode; nothing
    # FT2232H-only (0x8A, 0x8D, 0x97, ...) may appear
    assert ops <= KNOWN_OPCODES | {p.SYNC_PROBE}


def test_every_stream_fits_one_usb_packet(fake, dev):
    fake.read_replies.extend([ADC_REPLY] * 2)
    for call in TRANSACTIONS.values():
        call(dev)
    dev.set_hv_enable(True)
    dev.failsafe()
    dev.close()
    assert max(len(s) for s in fake.streams) <= MAX_STREAM_BYTES


# --- failsafe ----------------------------------------------------------------

def test_failsafe_clears_enables_then_zeros_dacs(fake, dev):
    dev.set_hv_enable(True)
    start = len(fake.streams)
    assert dev.failsafe()
    streams = fake.streams[start:]
    assert streams[0] == bytes([0x80, 0x9B, 0x7B])
    dac_writes = [c.data for c in fake.commands(start) if c.op == p.CLOCK_BYTES_OUT]
    assert dac_writes == [bytes([ch, 0, 0]) for ch in DAC_CHANNELS]
    assert not fake.adbus & p.HV_EN_BOTH


def test_failsafe_never_raises_on_transport_failure(fake, dev):
    dev.set_hv_enable(True)
    fake.fail_writes = True
    assert dev.failsafe() is False
    assert not dev.initialized


def test_failsafe_reports_stuck_enables(fake, dev):
    dev.set_hv_enable(True)
    fake.enable_readback = p.HV_EN_BOTH
    assert dev.failsafe() is False


# --- temperature -------------------------------------------------------------

@pytest.mark.parametrize("msb, lsb, celsius", [
    (0x7D, 0x00, 125.0),
    (0x19, 0x10, 25.0625),
    (0x00, 0x00, 0.0),
    (0xFF, 0x80, -0.5),
    (0xC9, 0x00, -55.0),
])
def test_temperature_decoding(msb, lsb, celsius):
    assert decode_temperature_c(msb, lsb) == celsius


@pytest.mark.parametrize("msb, lsb", [(0xFF, 0xFF), (0x19, 0x11), (0x7F, 0xF0), (0x80, 0x00)])
def test_temperature_rejects_impossible_readings(msb, lsb):
    with pytest.raises(TemperatureError):
        decode_temperature_c(msb, lsb)


def test_temperature_error_shows_both_bytes():
    with pytest.raises(TemperatureError, match="MSB 0x19 LSB 0x71"):
        decode_temperature_c(0x19, 0x71)


def test_temperature_read(fake, dev):
    assert dev.read_temperature_c() == 25.0625


def test_temperature_read_stream(fake, dev):
    start = len(fake.streams)
    dev.read_temperature_c()
    assert fake.streams[start:] == [bytes([
        0x86, 0x03, 0x00,
        0x80, 0x98, 0x7B,        # clock and data low
        0x82, 0x08, 0x08,        # TSCS high
        0x80, 0x98, 0x7B,
        0x80, 0x98, 0x7B,
        0x80, 0x99, 0x7B,        # clock high
        0x12, 0x07, 0x00,        # address 00h, 8 bits
        0x80, 0x99, 0x7B,        # "INPUTMODE"
        0x20, 0x02, 0x00,        # 3 bytes in
        0x82, 0x00, 0x08,        # TSCS low
    ])]


def test_temperature_refuses_unconfigured_sensor(fake, dev):
    fake.ds1722[0] = 0xE3                    # as found on hardware: shut down
    with pytest.raises(TemperatureError, match="not configured"):
        dev.read_temperature_c()


def test_temperature_detects_shifted_frame(fake, dev):
    fake.ds1722 = [0x74, 0x08, 0x0C]         # 0xE8 10 19 shifted one bit
    with pytest.raises(TemperatureError, match="misaligned"):
        dev.read_temperature_c()


def test_configure_then_read(fake, dev):
    fake.ds1722[0] = 0xE3
    dev.configure_temperature_sensor()
    assert fake.ds1722[0] == 0xE8
    assert dev.read_temperature_c() == 25.0625


def test_temperature_config_stream(fake, dev):
    start = len(fake.streams)
    dev.configure_temperature_sensor()
    cmds = fake.commands(start)
    assert [c.op for c in cmds] == [
        p.SET_DIVISOR, p.SET_ADBUS, p.SET_ACBUS, p.SET_ADBUS, p.SET_ADBUS, p.SET_ADBUS,
        p.CLOCK_BYTES_OUT, p.SET_ACBUS]
    assert cmds[1].args[0] & p.CLK == 0 and cmds[2].args[0] & p.TSCS
    assert cmds[5].args[0] & p.CLK
    assert cmds[6].data == bytes([0x80, 0xE8])
    assert not cmds[7].args[0] & p.TSCS


# --- threading and teardown --------------------------------------------------

def test_other_threads_are_refused(dev):
    errors = []

    def use():
        for call in (dev.read_gpio, dev.failsafe, lambda: dev.set_hv_enable(False)):
            try:
                call()
            except RuntimeError as exc:
                errors.append(exc)

    worker = threading.Thread(target=use)
    worker.start()
    worker.join()
    assert len(errors) == 3


def test_close_runs_failsafe_first(fake, dev):
    dev.set_hv_enable(True)
    assert dev.close() is True
    assert fake.closed
    assert not fake.adbus & p.HV_EN_BOTH
    assert not dev.initialized


def test_channel_tables():
    assert DAC_CHANNELS == (0x18, 0x19)
    assert ADC_CHANNELS == (0xD0, 0xF0)
