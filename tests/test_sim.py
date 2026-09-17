"""The simulator, driven through the real device layer."""

import pytest

from minix import protocol as p
from minix.device import (
    HvEnableError, InterlockOpenError, MiniX, NotInitializedError, TemperatureError,
    decode_temperature_c,
)
from minix.sim import SimFaults, SimHarness, SimTransport
from minix.transport import TransportError


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sim(clock):
    return SimTransport(clock=clock)


@pytest.fixture
def dev(sim):
    device = MiniX(sim)
    device.initialize()
    yield device
    assert sim.violations == []


def kv_counts(kv):
    return p.volts_to_counts(kv / p.HV_FACTOR_50KV)


def ua_counts(ua):
    return p.volts_to_counts(ua / p.CURRENT_FACTOR)


def energize(dev, clock, kv=15.0, ua=10.0, settle=3.0):
    dev.write_dac(p.DAC_HV, 0)
    dev.write_dac(p.DAC_CURRENT, 0)
    dev.set_hv_enable(True)
    dev.write_dac(p.DAC_CURRENT, ua_counts(ua))
    dev.write_dac(p.DAC_HV, kv_counts(kv))
    clock.advance(settle)


def monitors(dev):
    hv = dev.read_adc(p.ADC_HV).volts * p.HV_FACTOR_50KV
    ua = dev.read_adc(p.ADC_CURRENT).volts * p.CURRENT_FACTOR
    return hv, ua


# --- idle behaviour ----------------------------------------------------------

def test_idle_gpio_matches_hardware(dev):
    state = dev.read_gpio()
    assert (state.adbus, state.acbus) == (0x1F, 0x05)


def test_acbus_high_bits_are_noise_and_masked(sim, dev):
    raw = {sim.exchange(bytes([p.GET_ACBUS]), 1)[0] for _ in range(50)}
    assert raw & {0x45, 0x85}                       # noise shows up raw
    assert all(v & 0x0F == 0x05 for v in raw)
    assert all(dev.read_gpio().acbus == 0x05 for _ in range(20))


def test_idle_monitors_are_near_zero_and_well_framed(dev):
    counts = [dev.read_adc(ch).counts for _ in range(200) for ch in (p.ADC_HV, p.ADC_CURRENT)]
    assert max(counts) < 30
    assert 0 in counts                               # zero is a normal reading


def test_sync_check(dev):
    dev.check_sync()


def test_noise_is_reproducible(clock):
    runs = []
    for _ in range(2):
        device = MiniX(SimTransport(clock=clock, seed=7))
        device.initialize()
        runs.append([device.read_adc(p.ADC_HV).counts for _ in range(20)])
    assert runs[0] == runs[1]


# --- supply ------------------------------------------------------------------

def test_dac_writes_set_registers(sim, dev):
    dev.write_dac(p.DAC_HV, 1200)
    dev.write_dac(p.DAC_CURRENT, 200)
    assert (sim.hv_dac, sim.current_dac) == (1200, 200)
    assert sim.hv_setpoint_kv == pytest.approx(15.0)
    assert sim.current_setpoint_ua == pytest.approx(10.0)


def test_supply_follows_setpoints_only_when_enabled(sim, dev, clock):
    dev.write_dac(p.DAC_HV, kv_counts(30))
    dev.write_dac(p.DAC_CURRENT, ua_counts(50))
    clock.advance(5)
    sim.advance()
    assert (sim.kv, sim.ua) == (0.0, 0.0)
    dev.set_hv_enable(True)
    clock.advance(5)
    hv, ua = monitors(dev)
    assert hv == pytest.approx(30, abs=0.5)
    assert ua == pytest.approx(50, abs=1.0)


def test_readback_matches_the_hardware_measurement(dev, clock):
    energize(dev, clock, kv=15, ua=10)
    hv = [dev.read_adc(p.ADC_HV).counts for _ in range(200)]
    ua = [dev.read_adc(p.ADC_CURRENT).counts for _ in range(200)]
    # §10.3: 1204 for 1200 commanded, 208 for 200
    assert sum(hv) / len(hv) == pytest.approx(1205, abs=2)
    assert sum(ua) / len(ua) == pytest.approx(208, abs=2)


def test_supply_settles_gradually(sim, dev, clock):
    energize(dev, clock, kv=40, ua=20, settle=0.0)
    clock.advance(0.3)                               # one time constant
    sim.advance()
    assert sim.kv == pytest.approx(40 * (1 - 1 / 2.718281828), rel=0.01)


def test_monx_asserts_after_delay_and_drops_with_enable(sim, dev, clock):
    dev.set_hv_enable(True)
    assert not dev.read_gpio().tube_ready
    clock.advance(0.31)
    assert dev.read_gpio().tube_ready
    assert not dev.set_hv_enable(False).tube_ready


def test_hv_off_decays(sim, dev, clock):
    energize(dev, clock)
    dev.set_hv_enable(False)
    clock.advance(3)
    sim.advance()
    assert sim.kv < 0.1 and sim.ua < 0.1


# --- interlock ---------------------------------------------------------------

def test_open_interlock_refuses_enable(sim, dev):
    sim.set_interlock(False)
    assert not dev.read_gpio().interlock_closed
    with pytest.raises(InterlockOpenError):
        dev.set_hv_enable(True)
    assert not sim.hv_enabled


def test_opening_interlock_cuts_the_supply(sim, dev, clock):
    energize(dev, clock)
    sim.set_interlock(False)
    clock.advance(3)
    state = dev.read_gpio()
    assert not state.interlock_closed
    assert state.hv_enabled                          # the pins still say on
    assert not state.tube_ready
    hv, ua = monitors(dev)
    assert hv < 1 and ua < 1


# --- power tracking ----------------------------------------------------------

def test_peak_commanded_power_counts_intermediate_states(sim, dev, clock):
    energize(dev, clock, kv=40, ua=90)               # 3600 mW
    sim.reset_peaks()
    # 40 kV -> 20 kV and 90 uA -> 190 uA, current first: passes 40 kV x 190 uA
    dev.write_dac(p.DAC_CURRENT, ua_counts(190))
    dev.write_dac(p.DAC_HV, kv_counts(20))
    assert sim.max_commanded_mw == pytest.approx(40 * 190, rel=0.01)


def test_peak_commanded_power_ignores_disabled_writes(sim, dev):
    dev.write_dac(p.DAC_HV, p.COUNTS_MAX)
    dev.write_dac(p.DAC_CURRENT, p.COUNTS_MAX)
    assert sim.max_commanded_mw == 0.0


def test_peak_actual_power(sim, dev, clock):
    energize(dev, clock, kv=20, ua=100)
    sim.advance()
    assert sim.max_actual_mw == pytest.approx(2000, rel=0.02)


# --- DS1722 ------------------------------------------------------------------

def test_sensor_starts_unconfigured(dev):
    with pytest.raises(TemperatureError, match="not configured"):
        dev.read_temperature_c()


def test_configure_then_read_after_conversion(sim, dev, clock):
    dev.configure_temperature_sensor()
    assert sim.ds1722_config == 0xE8
    clock.advance(1.3)
    assert dev.read_temperature_c() == 27.0


def test_reading_before_first_conversion_is_the_old_value(dev, clock):
    dev.configure_temperature_sensor()
    clock.advance(0.5)
    assert dev.read_temperature_c() == 25.0


def test_board_heats_with_tube_power(dev, clock):
    dev.configure_temperature_sensor()
    energize(dev, clock, kv=40, ua=100)              # 4 W -> +2 °C
    clock.advance(5)
    assert dev.read_temperature_c() == pytest.approx(29.0, abs=0.1)


def test_config_is_volatile(clock):
    for _ in range(2):                               # two power-ups
        device = MiniX(SimTransport(clock=clock))
        device.initialize()
        with pytest.raises(TemperatureError, match="not configured"):
            device.read_temperature_c()
        device.configure_temperature_sensor()


def ds1722_stream(clk_at_select, clk_during, body):
    """A DS1722 transaction with the clock held as given (see tools/ds1722_probe.py)."""
    def adbus(clk):
        state = (p.ADBUS_INIT & ~(p.CLK | p.DATA_OUT)) | (p.CLK if clk else 0)
        return bytes([p.SET_ADBUS, state, p.ADBUS_DIRECTION])
    return (adbus(clk_at_select) + bytes([p.SET_ACBUS, p.TSCS, p.ACBUS_DIRECTION])
            + adbus(clk_during) + body + bytes([p.SET_ACBUS, 0x00, p.ACBUS_DIRECTION]))


def test_one_level_clock_misframes_like_the_hardware(sim, dev):
    read = bytes([p.CLOCK_BITS_OUT_TS, 0x07, 0x00, p.CLOCK_BYTES_IN, 0x02, 0x00])
    assert sim.exchange(ds1722_stream(0, 1, read), 3) == bytes.fromhex("e3 00 19")
    assert sim.exchange(ds1722_stream(0, 0, read), 3) == bytes.fromhex("71 80 0c")
    read_01 = bytes([p.CLOCK_BITS_OUT_TS, 0x07, 0x01, p.CLOCK_BYTES_IN, 0x01, 0x00])
    assert sim.exchange(ds1722_stream(0, 0, read_01), 2) == bytes.fromhex("71 80")
    write = bytes([p.CLOCK_BYTES_OUT, 0x01, 0x00, 0x80, 0xE8])
    sim.write(ds1722_stream(0, 0, write))
    assert sim.ds1722_config == 0xE3                 # misframed write ignored
    sim.write(ds1722_stream(0, 1, write))
    assert sim.ds1722_config == 0xE8


# --- protocol violations -----------------------------------------------------

def test_two_chips_selected_is_a_violation(sim, dev):
    both = (p.ADBUS_INIT & ~(p.ADCS | p.DACS)) | p.CLK
    sim.write(bytes([p.SET_ADBUS, both, p.ADBUS_DIRECTION,
                     p.CLOCK_BYTES_OUT, 0x02, 0x00, p.DAC_HV, 0xFF, 0xF0,
                     p.SET_ADBUS, p.ADBUS_INIT, p.ADBUS_DIRECTION]))
    assert sim.hv_dac == 0
    assert any("selected" in v for v in sim.violations)
    sim.violations.clear()


def test_single_enable_bit_is_a_violation(sim, dev):
    sim.write(bytes([p.SET_ADBUS, p.ADBUS_INIT | p.HV_EN_A, p.ADBUS_DIRECTION]))
    assert not sim.supply_on
    assert any("one HV enable" in v for v in sim.violations)
    sim.violations.clear()


def test_invalid_opcode_is_echoed(sim, dev):
    assert sim.exchange(bytes([0x8A]), 2) == bytes([0xFA, 0x8A])


def test_incomplete_command_waits_for_the_rest(sim, dev):
    sim.write(bytes([p.SET_ADBUS, p.ADBUS_INIT]))
    sim.write(bytes([p.ADBUS_DIRECTION]))
    assert dev.read_gpio().adbus == 0x1F


def test_unread_reply_bytes_are_purged(sim, dev):
    sim.write(bytes([p.SYNC_PROBE]))                 # leaves 0xFA 0xAB unread
    assert dev.read_gpio().adbus == 0x1F


# --- fault injection ---------------------------------------------------------

def test_io_failure(sim, dev):
    sim.fail_io = True
    with pytest.raises(TransportError):
        dev.read_gpio()
    assert not dev.initialized
    sim.fail_io = False
    dev.initialize()
    assert dev.read_gpio().hv_disabled


def test_short_reply(sim, dev):
    sim.drop_reply_bytes = 1
    with pytest.raises(TransportError, match="short read"):
        dev.read_adc(p.ADC_HV)
    sim.drop_reply_bytes = 0
    with pytest.raises(NotInitializedError):
        dev.read_adc(p.ADC_HV)


def test_stuck_enables(sim, dev):
    dev.set_hv_enable(True)
    sim.stuck_enable_readback = p.HV_EN_BOTH
    with pytest.raises(HvEnableError, match="check the tube"):
        dev.set_hv_enable(False)
    assert not sim.hv_enabled                        # the pins did clear


def test_close_releases_pins(sim, dev, clock):
    energize(dev, clock)
    assert dev.close() is True
    assert not sim.supply_on
    with pytest.raises(TransportError):
        sim.write(bytes([p.GET_ADBUS]))


def test_temperature_encoding_round_trip():
    from minix.sim import _encode_temperature
    for celsius, bits, expected in [(25.0, 12, 25.0), (-0.5, 12, -0.5), (27.3, 9, 27.0),
                                    (27.3, 12, 27.25), (-10.3, 8, -11.0)]:
        reg = _encode_temperature(celsius, bits)
        assert decode_temperature_c(reg >> 8, reg & 0xFF) == expected


# --- harness -----------------------------------------------------------------

def test_harness_applies_faults_to_new_simulators():
    harness = SimHarness(settle_tau_s=0.05, monx_delay_s=0.02)
    harness.set_fault("interlock_closed", False)
    harness.set_fault("tube_never_ready", True)
    sim = harness.create("01300036")
    assert harness.current is sim
    assert not sim.interlock_closed and sim.monx_delay_s == 1e9
    assert sim.settle_tau_s == 0.05


def test_harness_applies_changes_to_the_current_simulator():
    harness = SimHarness(monx_delay_s=0.02)
    sim = harness.create("01300036")
    for name in SimFaults.names():
        harness.set_fault(name, not getattr(SimFaults(), name))
    assert (sim.interlock_closed, sim.monx_delay_s, sim.settle_tau_s,
            sim.stuck_enable_readback, sim.fail_io) == (False, 1e9, 1e9, p.HV_EN_BOTH, True)
    for name in SimFaults.names():
        harness.set_fault(name, getattr(SimFaults(), name))
    assert (sim.interlock_closed, sim.monx_delay_s, sim.settle_tau_s,
            sim.stuck_enable_readback, sim.fail_io) == (True, 0.02, 0.3, None, False)


def test_harness_rejects_unknown_faults():
    with pytest.raises(AttributeError):
        SimHarness().set_fault("gremlins", True)


def test_every_fault_has_a_label():
    assert set(SimFaults.LABELS) == set(SimFaults.names())
