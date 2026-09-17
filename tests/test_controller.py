"""The controller, driven deterministically against the simulator."""

import threading
import time

import pytest

from minix import protocol as p
from minix.config import PollingConfig, SafetyConfig, Settings, UnitEntry
from minix.controller import Controller, Level, Session, State
from minix.policy.banding import PowerBand
from minix.sim import SimTransport
from minix.transport import TransportError

SERIAL = "01300036"


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self._hooks = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        due = [h for h in self._hooks if h[0] <= self.now]
        self._hooks = [h for h in self._hooks if h[0] > self.now]
        for _, fn in due:
            fn()

    def at(self, offset, fn):
        """Run fn once, the first time a sleep passes now + offset."""
        self._hooks.append((self.now + offset, fn))


class Recorder:
    def __init__(self):
        self.statuses = []
        self.events = []

    def status(self, status):
        self.statuses.append(status)

    def event(self, event):
        self.events.append(event)

    def kinds(self):
        return [e.kind for e in self.events]

    def last(self, kind):
        return next(e for e in reversed(self.events) if e.kind == kind)


def settings(watts=10.0, **safety):
    return Settings(units={SERIAL: UnitEntry(watts, "test")}, safety=SafetyConfig(**safety))


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sim(clock):
    return SimTransport(SERIAL, clock=clock)


@pytest.fixture
def rec():
    return Recorder()


def make_session(clock, sim, rec, config=None):
    opened = []

    def factory(serial):
        opened.append(serial)
        return sim

    session = Session(config or settings(), transport_factory=factory, listener=rec,
                      clock=clock, sleep=clock.sleep)
    session.opened = opened
    return session


@pytest.fixture
def session(clock, sim, rec):
    return make_session(clock, sim, rec)


def run(session, clock, seconds):
    """Let the worker loop run for a while."""
    end = clock.now + seconds
    while clock.now < end:
        clock.sleep(max(session.seconds_until_due(), 0.01))
        session.tick()


def connect(session, clock):
    session.connect(SERIAL)
    run(session, clock, 3.5)          # past the interlock-clear delay
    assert session.state is State.IDLE


def energize(session, clock, kv=15.0, ua=10.0):
    session.energize(kv, ua, session.interlock_epoch)
    assert session.state is State.ON, session.status().fault


@pytest.fixture
def on(session, clock, sim):
    connect(session, clock)
    energize(session, clock)
    sim.reset_peaks()
    return session


# --- connecting --------------------------------------------------------------

def test_unconfigured_unit_is_not_opened(session, rec):
    session.connect("09999999")
    assert session.state is State.DISCONNECTED
    assert session.opened == []
    event = rec.last("rating_required")
    assert event.data["serial"] == "09999999"


def test_connect(session, clock, sim, rec):
    session.connect(SERIAL)
    status = session.status()
    assert status.state is State.IDLE and status.connected
    assert status.unit.watt_max_w == 10.0
    assert "10 W" in rec.last("connected").message
    assert sim.ds1722_config == p.TS_CONFIG_12BIT_CONTINUOUS
    assert (sim.hv_dac, sim.current_dac) == (0, 0)
    assert status.controls_locked                 # interlock-clear delay after connect
    run(session, clock, 3.5)
    status = session.status()
    assert not status.controls_locked and status.can_energize
    assert status.temperature_c == 27.0
    assert status.kv is not None and status.kv < 1
    assert sim.violations == []


def test_connect_with_open_interlock_keeps_controls_locked(session, clock, sim):
    sim.set_interlock(False)
    session.connect(SERIAL)
    run(session, clock, 5)
    assert session.status().controls_locked


def test_connect_failure(clock, rec):
    def factory(serial):
        raise TransportError("no such device")
    session = Session(settings(), transport_factory=factory, listener=rec, clock=clock,
                      sleep=clock.sleep)
    session.connect(SERIAL)
    assert session.state is State.DISCONNECTED
    assert "no such device" in rec.last("connect_failed").message


def test_connect_twice_is_refused(session, clock, rec):
    connect(session, clock)
    session.connect(SERIAL)
    assert rec.last("refused").message == "already connected"


def test_disconnect(session, clock, sim, rec):
    connect(session, clock)
    session.disconnect()
    assert session.state is State.DISCONNECTED
    assert not session.status().connected
    with pytest.raises(TransportError):
        sim.write(bytes([p.GET_ADBUS]))


def test_disconnect_while_on_switches_hv_off_first(on, sim, rec):
    on.disconnect()
    assert rec.kinds()[-2:] == ["hv_off", "disconnected"]
    assert not sim.supply_on


# --- HV on and off -----------------------------------------------------------

def test_energize(session, clock, sim, rec):
    connect(session, clock)
    sim.reset_peaks()
    energize(session, clock, 15, 10)
    assert (sim.hv_dac, sim.current_dac) == (1200, 200)
    assert sim.supply_on and sim.tube_ready
    assert rec.last("hv_on").message == "HV on at 15.00 kV, 10.00 µA"
    assert [s.state for s in rec.statuses].count(State.ENERGIZING) > 0
    run(session, clock, 5)
    status = session.status()
    assert status.state is State.ON and status.enables_on and status.tube_ready
    # single readings are noisy (σ ≈ 15 counts on hardware); averages less so
    assert status.kv == pytest.approx(15, abs=0.8)
    assert status.kv_average == pytest.approx(15, abs=0.4)
    assert status.ua_average == pytest.approx(10.4, abs=1.0)
    assert status.range.testing and status.range.ok
    assert status.band is PowerBand.NORMAL
    assert status.power_mw == pytest.approx(156, abs=45)
    assert sim.violations == []


def test_energize_ramps_from_zero(session, clock, sim):
    connect(session, clock)
    sim.hv_dac = sim.current_dac = 4000      # as if something left them high
    states = []
    write, exchange = sim.write, sim.exchange

    def spy_write(data):
        write(data)
        states.append((sim.hv_enabled, sim.hv_dac, sim.current_dac))

    def spy_exchange(data, n):
        rx = exchange(data, n)
        states.append((sim.hv_enabled, sim.hv_dac, sim.current_dac))
        return rx
    sim.write, sim.exchange = spy_write, spy_exchange
    energize(session, clock, 20, 50)
    first_enabled = next(s for s in states if s[0])
    assert first_enabled[1:] == (0, 0)


def test_energize_adjusts_and_reports(session, clock, sim, rec):
    connect(session, clock)
    energize(session, clock, 50, 250)
    messages = [e.message for e in rec.events if e.kind == "adjusted"]
    assert any("maximum" in m for m in messages)
    assert any("power limit" in m for m in messages)
    assert sim.max_commanded_mw < 9950


def test_deenergize(on, clock, sim, rec):
    run(on, clock, 2)
    assert on.status().kv is not None
    on.deenergize()
    assert on.state is State.IDLE
    assert rec.last("hv_off")
    status = on.status()                       # no readings from before the switch
    assert (status.kv, status.ua, status.power_mw, status.band) == (None, None, None, None)
    run(on, clock, 0.6)
    assert on.status().kv < 1
    assert (sim.hv_dac, sim.current_dac) == (0, 0) and not sim.hv_enabled
    run(on, clock, 10)                         # HV-off tests start after 7 s
    status = on.status()
    assert status.range.testing and status.range.ok


def test_energize_refused_unless_idle_and_unlocked(session, clock, rec):
    session.energize(15, 10, 0)
    assert "disconnected" in rec.last("refused").message
    session.connect(SERIAL)
    session.energize(15, 10, session.interlock_epoch)
    assert "just restored" in rec.last("refused").message
    assert session.state is State.IDLE


def test_invalid_request_is_refused(session, clock, rec):
    connect(session, clock)
    session.energize(float("nan"), 10, session.interlock_epoch)
    assert session.state is State.IDLE
    assert "finite" in rec.last("refused").message


def test_missing_monx_is_a_fault(session, clock, sim, rec):
    connect(session, clock)
    sim.monx_delay_s = 1e9
    session.energize(15, 10, session.interlock_epoch)
    status = session.status()
    assert status.state is State.FAULT
    assert "MONX" in status.fault
    assert not sim.hv_enabled and (sim.hv_dac, sim.current_dac) == (0, 0)
    sim.monx_delay_s = 0.3
    session.clear_fault()
    assert session.state is State.IDLE


def test_slow_supply_warns_but_stays_on(session, clock, sim, rec):
    connect(session, clock)
    sim.settle_tau_s = 1e6
    energize(session, clock, 30, 50)
    assert "settle_timeout" in rec.kinds()
    run(session, clock, 3)
    status = session.status()
    assert status.state is State.ON             # the range check is an indicator only
    assert status.range.testing and not status.range.ok


# --- setpoint changes --------------------------------------------------------

def test_commit_while_on(on, clock, sim, rec):
    on.commit(30, 100)
    assert on.state is State.ON
    assert (sim.hv_setpoint_kv, sim.current_setpoint_ua) == (pytest.approx(30), pytest.approx(100))
    assert "applied" in rec.last("setpoints").message
    run(on, clock, 5)
    assert on.status().range.ok


def test_commit_while_idle_stores_only(session, clock, sim, rec):
    connect(session, clock)
    session.commit(25, 40)
    assert (sim.hv_dac, sim.current_dac) == (0, 0)
    assert session.status().setpoints.kv == pytest.approx(25)
    assert "will apply" in rec.last("setpoints").message


def test_commit_order_keeps_power_under_the_limit(clock, sim, rec):
    session = make_session(clock, sim, rec, settings(watts=4.0))
    connect(session, clock)
    energize(session, clock, 20, 150)          # 3000 mW
    sim.reset_peaks()
    session.commit(35, 100)                    # 35 x 150 would exceed 4 W
    assert session.state is State.ON
    assert sim.max_commanded_mw < 3950


# --- interlock ---------------------------------------------------------------

def test_interlock_opening_while_on(on, clock, sim, rec):
    epoch = on.interlock_epoch
    sim.set_interlock(False)
    run(on, clock, 0.2)
    status = on.status()
    assert status.state is State.IDLE
    assert status.interlock_epoch == epoch + 1
    assert status.controls_locked
    assert not sim.hv_enabled and (sim.hv_dac, sim.current_dac) == (0, 0)
    assert rec.kinds().count("interlock_opened") == 1

    sim.set_interlock(True)
    run(on, clock, 0.2)
    assert on.status().controls_locked          # restored, but still locked
    assert on.state is State.IDLE and not sim.hv_enabled
    run(on, clock, 3.0)
    assert not on.status().controls_locked


def test_stale_confirmation_is_refused(session, clock, sim, rec):
    connect(session, clock)
    epoch = session.interlock_epoch            # the UI shows a confirmation dialog
    sim.set_interlock(False)
    run(session, clock, 0.2)
    sim.set_interlock(True)
    run(session, clock, 5)
    session.energize(15, 10, epoch)            # the operator answers yes afterwards
    assert session.state is State.IDLE
    assert "request again" in rec.last("refused").message
    energize(session, clock)                   # a fresh request works


def test_interlock_opening_during_energize(session, clock, sim, rec):
    connect(session, clock)
    clock.at(0.3, lambda: sim.set_interlock(False))
    session.energize(40, 100, session.interlock_epoch)
    assert session.state is State.IDLE
    assert rec.last("aborted").message == "interlock opened"
    assert not sim.hv_enabled and (sim.hv_dac, sim.current_dac) == (0, 0)


def test_interlock_open_when_enabling(session, clock, sim, rec):
    connect(session, clock)
    session.tick()
    sim.set_interlock(False)                   # opens before the next GPIO poll
    session.energize(15, 10, session.interlock_epoch)
    assert session.state is State.IDLE
    assert "interlock" in rec.last("aborted").message
    assert not sim.hv_enabled


# --- emergency stop and abort ------------------------------------------------

def test_estop_while_on(on, clock, sim, rec):
    on.estop.set()
    on.tick()
    assert on.state is State.IDLE
    assert rec.last("estop").level is Level.ERROR
    assert not sim.hv_enabled and (sim.hv_dac, sim.current_dac) == (0, 0)
    assert not on.estop.is_set()


def test_estop_during_energize(session, clock, sim, rec):
    connect(session, clock)
    clock.at(0.3, session.estop.set)
    session.energize(40, 100, session.interlock_epoch)
    assert session.state is State.IDLE
    assert "estop" in rec.kinds() and "hv_on" not in rec.kinds()
    assert not sim.hv_enabled


def test_estop_during_deenergize(on, clock, sim, rec):
    clock.at(0.1, on.estop.set)
    on.deenergize()
    assert on.state is State.IDLE
    assert "estop" in rec.kinds()
    assert not sim.hv_enabled and (sim.hv_dac, sim.current_dac) == (0, 0)


def test_estop_keeps_a_fault(session, clock, sim, rec):
    connect(session, clock)
    sim.monx_delay_s = 1e9
    session.energize(15, 10, session.interlock_epoch)
    session.estop.set()
    session.tick()
    assert session.state is State.FAULT


def test_abort_during_commit(on, clock, sim, rec):
    clock.at(0.3, on.abort.set)
    on.commit(40, 150)
    assert on.state is State.IDLE
    assert rec.last("aborted").message == "HV off requested"
    assert not sim.hv_enabled


def test_idle_session_ignores_a_stale_abort(session, clock, rec):
    connect(session, clock)
    session.abort.set()
    session.deenergize()
    energize(session, clock)


# --- faults ------------------------------------------------------------------

def test_stuck_enables_on_deenergize(on, sim, rec):
    sim.stuck_enable_readback = p.HV_EN_BOTH
    on.deenergize()
    status = on.status()
    assert status.state is State.FAULT
    assert "check the tube" in status.fault
    on.clear_fault()
    assert on.state is State.FAULT              # still reads set
    sim.stuck_enable_readback = None
    on.clear_fault()
    assert on.state is State.IDLE


def test_enables_set_while_idle_is_a_fault(session, clock, sim):
    connect(session, clock)
    sim.stuck_enable_readback = p.HV_EN_BOTH
    run(session, clock, 0.2)
    assert session.state is State.FAULT
    assert "while HV is off" in session.status().fault


def test_enables_dropping_while_on_is_a_fault(on, clock, sim):
    sim.stuck_enable_readback = 0x00
    run(on, clock, 0.2)
    assert on.state is State.FAULT
    assert "dropped" in on.status().fault


def test_single_enable_bit_is_a_fault(on, clock, sim):
    sim.stuck_enable_readback = p.HV_EN_A
    run(on, clock, 0.2)
    assert on.state is State.FAULT
    assert not sim.hv_enabled


def test_repeated_framing_errors_are_a_fault(on, clock, sim, rec):
    sim.adc_framing_errors = 1
    run(on, clock, 1)
    assert on.state is State.ON                 # one is tolerated
    assert "framing" in rec.kinds()
    sim.adc_framing_errors = 3
    run(on, clock, 3)
    assert on.state is State.FAULT
    assert "framing" in on.status().fault
    assert not sim.hv_enabled


def test_lost_device(on, clock, sim, rec):
    sim.fail_io = True
    run(on, clock, 0.2)
    status = on.status()
    assert status.state is State.FAULT and not status.connected
    assert status.interlock_closed is None and status.enables_on is None
    assert "device_lost" in rec.kinds()
    on.clear_fault()
    assert on.state is State.DISCONNECTED


def test_lost_device_during_energize(session, clock, sim, rec):
    connect(session, clock)
    clock.at(0.3, lambda: setattr(sim, "fail_io", True))
    session.energize(15, 10, session.interlock_epoch)
    status = session.status()
    assert status.state is State.FAULT and not status.connected


def test_monx_dropping_is_a_warning(on, clock, sim, rec):
    run(on, clock, 0.5)
    sim.monx_delay_s = 1e9
    sim._enabled_since = clock.now             # as if the supply restarted
    run(on, clock, 0.5)
    assert on.state is State.ON
    assert "tube_not_ready" in rec.kinds()


def test_temperature_sensor_is_reconfigured(on, clock, sim, rec):
    sim.ds1722_config = 0xE3                    # the sensor lost power
    run(on, clock, 12)
    assert "temperature" in rec.kinds()
    assert sim.ds1722_config == p.TS_CONFIG_12BIT_CONTINUOUS
    assert on.status().temperature_c is not None
    assert on.state is State.ON


def test_internal_error_forces_hv_off(on, sim):
    on.internal_error(RuntimeError("boom"))
    assert on.state is State.FAULT
    assert not sim.hv_enabled


def test_polling_rates(session, clock, sim):
    connect(session, clock)
    counts = {"gpio": 0, "adc": 0}
    original = sim.exchange

    def spy(data, n):
        if data == bytes([p.GET_ADBUS, p.GET_ACBUS]):
            counts["gpio"] += 1
        elif p.CLOCK_BITS_OUT in data:
            counts["adc"] += 1
        return original(data, n)
    sim.exchange = spy
    run(session, clock, 10)
    assert counts["gpio"] == pytest.approx(100, abs=3)    # 10 Hz
    assert counts["adc"] == pytest.approx(40, abs=3)      # two channels at 2 Hz


def test_monitors_are_read_in_fault(session, clock, sim):
    connect(session, clock)
    sim.monx_delay_s = 1e9
    session.energize(30, 100, session.interlock_epoch)
    assert session.state is State.FAULT
    assert session.status().kv is None
    run(session, clock, 1)
    status = session.status()
    assert status.kv is not None and status.kv < 30     # decaying toward zero
    run(session, clock, 5)
    assert session.status().kv < 1


def test_fault_state_does_not_spin(session, clock, sim):
    connect(session, clock)
    sim.stuck_enable_readback = p.HV_EN_BOTH
    run(session, clock, 0.2)
    assert session.state is State.FAULT
    assert session.seconds_until_due() > 0.05


# --- the threaded controller -------------------------------------------------

class ThreadRecorder(Recorder):
    def __init__(self):
        super().__init__()
        self.cond = threading.Condition()

    def status(self, status):
        with self.cond:
            super().status(status)
            self.cond.notify_all()

    def event(self, event):
        with self.cond:
            super().event(event)
            self.cond.notify_all()

    def wait_for(self, predicate, timeout=10.0):
        with self.cond:
            assert self.cond.wait_for(predicate, timeout), self.kinds()


def fast_settings():
    return Settings(units={SERIAL: UnitEntry(10.0)},
                    safety=SafetyConfig(interlock_clear_s=0.1),
                    polling=PollingConfig(gpio_hz=50, adc_hz=10, temp_hz=1))


def test_controller_thread():
    rec = ThreadRecorder()
    sims = []

    def factory(serial):
        sims.append(SimTransport(serial, settle_tau_s=0.02, monx_delay_s=0.01))
        return sims[-1]

    controller = Controller(fast_settings(), transport_factory=factory, listener=rec)
    controller.start()
    try:
        controller.connect(SERIAL)
        rec.wait_for(lambda: rec.statuses and rec.statuses[-1].can_energize)
        controller.energize(20, 50, rec.statuses[-1].interlock_epoch)
        rec.wait_for(lambda: "hv_on" in rec.kinds())
        assert sims[0].supply_on

        controller.emergency_stop()
        rec.wait_for(lambda: "estop" in rec.kinds())
        assert not sims[0].hv_enabled
    finally:
        assert controller.shutdown(timeout=10)
    assert "disconnected" in rec.kinds()


def test_controller_hv_off_cancels_a_queued_hv_on():
    rec = ThreadRecorder()
    gate = threading.Event()

    def factory(serial):
        gate.wait(5)                      # keep the worker busy connecting
        return SimTransport(serial, settle_tau_s=0.02, monx_delay_s=0.01)

    controller = Controller(fast_settings(), transport_factory=factory, listener=rec)
    controller.start()
    try:
        controller.connect(SERIAL)
        time.sleep(0.1)
        controller.energize(20, 50, 0)    # queued behind the connect
        controller.deenergize()           # the operator changes their mind
        gate.set()
        rec.wait_for(lambda: "cancelled" in rec.kinds())
        time.sleep(0.3)
        assert "hv_on" not in rec.kinds()
    finally:
        assert controller.shutdown(timeout=10)


def test_controller_survives_internal_errors():
    rec = ThreadRecorder()
    controller = Controller(fast_settings(), transport_factory=lambda s: SimTransport(s),
                            listener=rec)
    controller.start()
    try:
        controller._submit(1, lambda s: 1 / 0)
        controller.connect(SERIAL)
        rec.wait_for(lambda: "connected" in rec.kinds())
        assert controller.alive
    finally:
        assert controller.shutdown(timeout=10)
