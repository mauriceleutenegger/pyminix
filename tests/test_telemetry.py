import csv
import logging
from datetime import datetime, timedelta, timezone

import pytest

from minix.config import Settings, UnitEntry
from minix.controller import Event, FanOut, Level, Session, State, Status
from minix.sim import SimTransport
from minix.telemetry import (
    EVENT_COLUMNS, SAMPLE_COLUMNS, RunRecorder, setup_logging, software_version,
)

SERIAL = "01300036"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def wall(self):
        return datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=self.now)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def recorder(tmp_path, clock):
    return RunRecorder(tmp_path / "runs", sample_hz=1.0, wall_clock=clock.wall)


def make_session(clock, listener, sim=None):
    settings = Settings(units={SERIAL: UnitEntry(10.0, "label on the unit")})
    sims = []

    def factory(serial):
        sims.append(sim or SimTransport(serial, clock=clock))
        return sims[-1]
    session = Session(settings, transport_factory=factory, listener=listener,
                      clock=clock, sleep=clock.sleep)
    session.sims = sims
    return session


def run(session, clock, seconds):
    end = clock.now + seconds
    while clock.now < end:
        clock.sleep(max(session.seconds_until_due(), 0.01))
        session.tick()


def read(path):
    with open(path, newline="") as f:
        lines = f.read().splitlines()
    comments = [line[2:] for line in lines if line.startswith("# ")]
    rows = list(csv.DictReader(line for line in lines if not line.startswith("#")))
    return comments, rows


def test_run_record(recorder, clock, tmp_path):
    session = make_session(clock, recorder)
    session.connect(SERIAL)
    assert recorder.recording
    samples_path, events_path = recorder.paths
    assert samples_path.name == "20260917-121640_01300036.csv"
    assert events_path.name == "20260917-121640_01300036_events.csv"

    run(session, clock, 4)
    session.energize(15, 10, session.interlock_epoch)
    run(session, clock, 5)
    session.deenergize()
    session.disconnect()
    assert not recorder.recording

    header, samples = read(samples_path)
    assert "serial: 01300036" in header
    assert "power rating W: 10.0" in header
    assert "rating source: controller reports MX50.10; configuration agrees" in header
    assert "device type: 2" in header
    assert "safety margin W: 0.05" in header
    assert "unit: Mini-X 01300036: MX50.10, 50 kV, 10 W rating" in header
    assert f"software: {software_version()}" in header
    assert list(samples[0]) == SAMPLE_COLUMNS

    states = [row["state"] for row in samples]
    for state in ("IDLE", "ENERGIZING", "ON", "DEENERGIZING"):
        assert state in states
    # a state change is always recorded, even within the sample interval
    changes = [i for i in range(1, len(states)) if states[i] != states[i - 1]]
    assert len(changes) >= 4

    on_rows = [row for row in samples if row["state"] == "ON"]
    assert 4 <= len(on_rows) <= 8                     # about 1 Hz for 5 s
    last_on = on_rows[-1]
    assert float(last_on["kv"]) == pytest.approx(15, abs=0.5)
    assert float(last_on["setpoint_kv"]) == pytest.approx(15)
    assert float(last_on["setpoint_ua"]) == pytest.approx(10)
    assert last_on["enables_on"] == "1" and last_on["tube_ready"] == "1"
    assert last_on["in_range"] == "1"
    assert last_on["band"] == "NORMAL"
    assert float(last_on["power_average_mw"]) == pytest.approx(155, abs=15)
    assert last_on["monx_drops"] == "0"
    assert last_on["temperature_c"] != ""
    assert last_on["time"].startswith("2026-09-17T12:")
    elapsed = [float(row["elapsed_s"]) for row in samples]
    assert elapsed == sorted(elapsed) and elapsed[0] >= 0

    _, events = read(events_path)
    assert list(events[0]) == EVENT_COLUMNS
    kinds = [row["kind"] for row in events]
    assert kinds[0] == "connected" and kinds[-1] == "disconnected"
    assert "hv_on" in kinds and "hv_off" in kinds
    assert events[0]["elapsed_s"] == "0.000"


def test_new_file_per_connection(recorder, clock):
    session = make_session(clock, recorder)
    session.connect(SERIAL)
    first = recorder.paths[0]
    session.disconnect()
    session.connect(SERIAL)                          # same second: a distinct name
    second = recorder.paths[0]
    session.disconnect()
    assert first != second and first.exists() and second.exists()
    assert second.name == first.name.replace(".csv", "-1.csv")


def test_nothing_recorded_outside_a_run(recorder, clock, tmp_path):
    # A non-OEM controller with no configured rating: asked for, not connected.
    session = make_session(clock, recorder, SimTransport("09999999", clock=clock, device_type=3))
    session.connect("09999999")
    assert not recorder.recording
    assert not (tmp_path / "runs").exists()


def test_lost_device_ends_the_run(recorder, clock):
    session = make_session(clock, recorder)
    session.connect(SERIAL)
    path = recorder.paths[0]
    session.sims[0].fail_io = True
    run(session, clock, 0.2)
    assert not recorder.recording
    _, samples = read(path)
    assert samples[-1]["state"] == "FAULT"
    assert samples[-1]["fault"] != ""


def test_fault_is_recorded(recorder, clock):
    session = make_session(clock, recorder)
    session.connect(SERIAL)
    run(session, clock, 4)
    session.sims[0].monx_delay_s = 1e9
    session.energize(15, 10, session.interlock_epoch)
    samples_path, events_path = recorder.paths
    session.disconnect()
    _, samples = read(samples_path)
    assert any("MONX" in row["fault"] for row in samples)
    _, events = read(events_path)
    assert any(row["kind"] == "fault" and row["level"] == "error" for row in events)


def test_sampling_is_throttled(recorder, clock):
    session = make_session(clock, recorder)
    session.connect(SERIAL)
    run(session, clock, 10.05)                       # statuses at 10 Hz
    path = recorder.paths[0]
    session.disconnect()
    _, samples = read(path)
    assert 10 <= len(samples) <= 12


def test_write_failure_stops_recording_quietly(recorder, clock):
    session = make_session(clock, recorder)
    session.connect(SERIAL)
    recorder._samples._handle.close()                # e.g. the disk went away
    run(session, clock, 4)                           # past the interlock-clear delay
    assert not recorder.recording
    assert "run recording stopped" in recorder.error
    session.energize(15, 10, session.interlock_epoch)
    assert session.state is State.ON                 # the controller is unaffected


def test_unwritable_directory(tmp_path, clock):
    blocker = tmp_path / "file"
    blocker.write_text("")
    recorder = RunRecorder(blocker / "runs", wall_clock=clock.wall)
    session = make_session(clock, recorder)
    session.connect(SERIAL)
    assert session.state is State.IDLE
    assert not recorder.recording and recorder.error


class Exploding:
    def status(self, status):
        raise RuntimeError("listener bug")

    def event(self, event):
        raise RuntimeError("listener bug")


def test_a_failing_listener_does_not_disturb_the_controller(clock):
    session = make_session(clock, Exploding())
    session.connect(SERIAL)
    run(session, clock, 4)
    session.energize(15, 10, session.interlock_epoch)
    assert session.state is State.ON


def test_fan_out_isolates_listeners(recorder, clock):
    session = make_session(clock, FanOut(Exploding(), recorder))
    session.connect(SERIAL)
    assert recorder.recording


def test_recorder_ignores_input_between_runs(recorder):
    recorder.status(Status(time=0.0, state=State.IDLE))
    recorder.event(Event(0.0, Level.INFO, "hv_off", "HV off"))
    assert not recorder.recording


def test_setup_logging(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = setup_logging(tmp_path / "logs")
        setup_logging(tmp_path / "logs")             # idempotent
        assert len([h for h in root.handlers if getattr(h, "_minix", False)]) == 2
        logging.getLogger("minix.test").warning("hello from the test")
        for handler in root.handlers:
            handler.flush()
        assert "hello from the test" in path.read_text()
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()
