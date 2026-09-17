"""Device controller.

Session owns the device and runs everything on one thread: the startup,
energize, commit and de-energize plans (policy.sequencing), the polling,
the interlock handling and the state machine. Controller runs a Session on
a worker thread and feeds it commands from a priority queue; nothing else
touches the device (§11). Both report through a Listener, called on the
worker thread; the UI adapts that to Qt signals.

Behaviour, with the §s of docs/protocol.md it follows:

* Polling at independent rates: GPIO (interlock, enables, MONX), both
  monitors, and the temperature. GPIO polling continues inside every plan
  wait, so the interlock is watched during setpoint changes too.
* Interlock (§7.3). Opening it switches HV off (or aborts a running plan
  and then switches HV off) and increments interlock_epoch. energize()
  refuses a request carrying an older epoch, so a confirmation raised
  before the interlock opened cannot take effect. After it closes, the
  controls stay locked for interlock_clear_s, and HV stays off.
* Emergency stop: device.failsafe() at the next opportunity, interrupting
  any plan, including a de-energize.
* Faults: HV is forced off with failsafe() and the session waits in
  FAULT for clear_fault(). Faults are: an enable-bit readback that does not
  match, MONX not asserting after the ramp, a single enable bit set, enable
  bits that change on their own, and repeated ADC framing errors. A
  transport failure also closes the device.
* Not faults, following the reference: out-of-range monitors (an
  indicator only, §6.4), a monitor that does not settle within the
  reference's wait (a warning), MONX dropping while on, and temperature
  problems (a warning; an unconfigured sensor is reconfigured).
* MONX while on (§7.2, §10.8): brief drops are common near 200 µA and do
  not show in the monitors, so they are only counted (monx_drops) and
  summarized once a minute. MONX staying low for monx_warning_s is a
  warning.
* Power indicator: the band follows the averaged power with hysteresis
  (policy.banding.PowerBandTracker), so noise at full power does not make
  it flicker (§10.8). power_mw is the latest single reading.
"""

from __future__ import annotations

import enum
import itertools
import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol

from . import protocol as p
from .config import ConfigError, Settings, UnitConfig, UnitNotConfigured
from .device import (
    DeviceError, FramingError, GpioState, HvEnableError, InterlockOpenError, MiniX,
    NotInitializedError, SyncError, TemperatureError, TemperatureNotConfigured,
)
from .policy.averaging import RunningAverage
from .policy.banding import PowerBand, PowerBandTracker
from .policy.limits import Commitment, DacSetpoints, commit_setpoints
from .policy.ranging import RangeChecker, RangeStatus, in_range
from .policy.sequencing import (
    ConfigureTemperature, PlanError, RequireMonx, SetEnable, Step, Wait, WaitForSettle,
    WriteDac, plan_commit, plan_deenergize, plan_energize, plan_startup,
)
from .transport import Transport, TransportError

log = logging.getLogger(__name__)

MAX_FRAMING_ERRORS = 3          # consecutive ADC framing errors before a fault
IDLE_TICK_S = 0.5               # loop period while disconnected
FIRST_TEMPERATURE_DELAY_S = 1.5 # a 12-bit conversion takes up to 1.2 s
MONX_SUMMARY_S = 60.0           # how often brief MONX drops are summarized


class State(enum.Enum):
    DISCONNECTED = "disconnected"
    IDLE = "HV off"
    ENERGIZING = "switching HV on"
    ON = "HV on"
    CHANGING = "changing setpoints"
    DEENERGIZING = "switching HV off"
    FAULT = "fault"


HV_ACTIVE = frozenset({State.ENERGIZING, State.ON, State.CHANGING})
# States in which the monitors and temperature are polled between commands.
# In FAULT they show whether the tube has really gone to zero.
MONITORED = frozenset({State.IDLE, State.ON, State.FAULT})


class Level(enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Event:
    time: float
    level: Level
    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Status:
    time: float
    state: State
    connected: bool = False
    serial: str | None = None
    unit: UnitConfig | None = None
    interlock_closed: bool | None = None
    interlock_epoch: int = 0
    controls_locked: bool = True
    enables_on: bool | None = None
    tube_ready: bool | None = None
    monx_low_s: float | None = None     # how long MONX has read low with HV on
    monx_drops: int = 0                 # MONX drops since HV was last switched
    kv: float | None = None
    ua: float | None = None
    kv_average: float | None = None
    ua_average: float | None = None
    power_mw: float | None = None       # latest single readings
    power_average_mw: float | None = None
    band: PowerBand | None = None       # from the averaged power, with hysteresis
    range: RangeStatus | None = None
    temperature_c: float | None = None
    setpoints: Commitment | None = None
    fault: str | None = None

    @property
    def can_energize(self) -> bool:
        return self.state is State.IDLE and not self.controls_locked

    @property
    def can_commit(self) -> bool:
        return self.state in (State.IDLE, State.ON)

    @property
    def can_deenergize(self) -> bool:
        return self.state in HV_ACTIVE


class Listener(Protocol):
    def status(self, status: Status) -> None: ...

    def event(self, event: Event) -> None: ...


class NullListener:
    def status(self, status: Status) -> None:
        pass

    def event(self, event: Event) -> None:
        pass


class FanOut:
    """Passes everything to several listeners; one failing does not affect the others."""

    def __init__(self, *listeners: Listener):
        self._listeners = listeners

    def status(self, status: Status) -> None:
        for listener in self._listeners:
            _call_listener(listener.status, status)

    def event(self, event: Event) -> None:
        for listener in self._listeners:
            _call_listener(listener.event, event)


def _call_listener(method: Callable[[Any], None], arg: Any) -> None:
    # A listener must never disturb the controller: an exception here could
    # otherwise abort a plan mid-way.
    try:
        method(arg)
    except Exception:
        log.exception("listener %r failed", method)


TransportFactory = Callable[[str], Transport]


class _Abort(Exception):
    def __init__(self, reason: str, estop: bool = False):
        super().__init__(reason)
        self.estop = estop


class _Fault(Exception):
    pass


_DEVICE_LOST = (TransportError, NotInitializedError, SyncError)


class Session:
    """The controller's state machine. Use from one thread only.

    estop and abort are the only members other threads may touch.
    """

    def __init__(self, settings: Settings, *, transport_factory: TransportFactory,
                 listener: Listener | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 estop: threading.Event | None = None,
                 abort: threading.Event | None = None):
        self._settings = settings
        self._factory = transport_factory
        self._listener = listener or NullListener()
        self._clock = clock
        self._sleep = sleep
        self.estop = estop or threading.Event()
        self.abort = abort or threading.Event()
        self.interlock_epoch = 0
        self._reset()

    # --- commands --------------------------------------------------------------

    def connect(self, serial: str) -> None:
        if self._state is not State.DISCONNECTED:
            self._event(Level.WARNING, "refused", "already connected")
            return
        try:
            unit = self._settings.unit(serial)
        except UnitNotConfigured:
            self._event(Level.WARNING, "rating_required",
                        f"Mini-X {serial} has no configured power rating", serial=serial)
            return
        except ConfigError as exc:
            self._event(Level.ERROR, "connect_failed", str(exc))
            return
        try:
            transport = self._factory(serial)
        except (TransportError, OSError) as exc:
            self._event(Level.ERROR, "connect_failed", str(exc))
            return
        self._dev = MiniX(transport)
        self._unit = unit
        self._serial = serial
        self._dac = DacSetpoints.zero(unit)
        self._range = RangeChecker(unit, self._settings.safety)
        self._band = PowerBandTracker(unit)
        self._state = State.IDLE      # provisional, so failures are handled as faults
        try:
            self._dev.initialize()
            if not self._run_plan(plan_startup(unit), abortable=False):
                return
            gpio = self._poll_gpio()
            if not gpio.hv_disabled:
                self._enter_fault("HV enable bits read set after initialization")
                return
        except _Fault as exc:
            self._enter_fault(str(exc))
            return
        except (*_DEVICE_LOST, DeviceError) as exc:
            self._lose_device(f"connection failed: {exc}")
            return
        now = self._clock()
        self._hv_switched(now)
        self._next_poll = dict.fromkeys(self._next_poll, now)
        self._next_poll["temp"] = now + FIRST_TEMPERATURE_DELAY_S
        self._event(Level.INFO, "connected", unit.describe(), serial=serial,
                    watt_max_w=unit.watt_max_w, rating_source=unit.source,
                    safety_margin_w=unit.safety_margin_w)
        self._publish()

    def disconnect(self) -> None:
        if self._state is State.DISCONNECTED:
            return
        if self._state in HV_ACTIVE and self._dev is not None:
            self._deenergize_now()
        confirmed = True
        if self._dev is not None:
            confirmed = self._close_device()
        self._reset()
        self._event(Level.INFO, "disconnected", "disconnected")
        if not confirmed:
            self._event(Level.ERROR, "check_tube",
                        "HV enables could not be confirmed clear on disconnect; "
                        "check the tube physically")
        self._publish()

    def energize(self, kv: float, ua: float, interlock_epoch: int) -> None:
        now = self._clock()
        if self._state is not State.IDLE:
            self._event(Level.WARNING, "refused", f"cannot switch HV on while {self._state.value}")
            return
        if interlock_epoch != self.interlock_epoch:
            self._event(Level.WARNING, "refused",
                        "the interlock opened after HV on was requested; request again")
            return
        if self._controls_locked(now):
            self._event(Level.WARNING, "refused", "the interlock is open or was just restored")
            return
        commitment = self._accept(kv, ua)
        if commitment is None:
            return
        self._set_state(State.ENERGIZING)
        self._hv_switched(now)
        steps = plan_energize(commitment.setpoints, self._unit,
                              self._settings.safety.monx_timeout_s)
        if self._run_plan(steps, abortable=True):
            self._expected = commitment.setpoints
            self._set_state(State.ON)
            self._event(Level.INFO, "hv_on",
                        f"HV on at {commitment.kv:.2f} kV, {commitment.ua:.2f} µA")

    def commit(self, kv: float, ua: float) -> None:
        if self._state is State.IDLE:
            commitment = self._accept(kv, ua)
            if commitment is not None:
                self._event(Level.INFO, "setpoints",
                            f"setpoints {commitment.kv:.2f} kV, {commitment.ua:.2f} µA "
                            "will apply when HV is switched on")
                self._publish()
            return
        if self._state is not State.ON:
            self._event(Level.WARNING, "refused", f"cannot change setpoints while {self._state.value}")
            return
        commitment = self._accept(kv, ua)
        if commitment is None:
            return
        try:
            steps = plan_commit(self._dac, commitment.setpoints, self._unit)
        except PlanError as exc:
            self._enter_fault(f"internal error: {exc}")
            return
        self._set_state(State.CHANGING)
        self._reset_averages()
        if self._run_plan(steps, abortable=True):
            self._expected = commitment.setpoints
            self._set_state(State.ON)
            self._event(Level.INFO, "setpoints",
                        f"setpoints {commitment.kv:.2f} kV, {commitment.ua:.2f} µA applied")

    def deenergize(self) -> None:
        self.abort.clear()
        if self._state is State.ON:
            self._deenergize_now()

    def clear_fault(self) -> None:
        if self._state is not State.FAULT:
            return
        if self._dev is None:
            self._reset()
            self._event(Level.INFO, "fault_cleared", "fault cleared; disconnected")
            self._publish()
            return
        try:
            if not self._dev.initialized:
                self._dev.initialize()
            gpio = self._dev.read_gpio()
        except (*_DEVICE_LOST, DeviceError) as exc:
            self._lose_device(f"controller lost: not usable: {exc}")
            return
        self._gpio = gpio
        if not gpio.hv_disabled:
            self._event(Level.ERROR, "fault", "HV enable bits still read set; check the tube physically")
            return
        self._fault = None
        self._framing_errors = 0
        self._clear_at = self._clock() + self._settings.safety.interlock_clear_s
        self._set_state(State.IDLE)
        self._event(Level.INFO, "fault_cleared", "fault cleared")

    def reload_settings(self, settings: Settings) -> None:
        """Use new settings; a connected unit keeps its configuration until reconnected."""
        self._settings = settings

    def tick(self) -> None:
        """Handle an emergency stop and run whatever polls are due."""
        if self.estop.is_set():
            self._emergency_stop()
        if self._dev is None:
            return
        now = self._clock()
        try:
            polled = False
            if now >= self._next_poll["gpio"]:
                self._poll_gpio()
                polled = True
                if self._state is State.ON and not self._interlock_closed:
                    self._event(Level.WARNING, "hv_off", "interlock opened: switching HV off")
                    self._deenergize_now()
            if self._state in MONITORED:
                if now >= self._next_poll["adc"]:
                    self._poll_monitors()
                    polled = True
                if now >= self._next_poll["temp"]:
                    self._poll_temperature()
                    polled = True
            if polled:
                self._publish()
        except _Fault as exc:
            self._enter_fault(str(exc))
        except _DEVICE_LOST as exc:
            self._lose_device(f"controller lost: {exc}")
        except DeviceError as exc:
            self._enter_fault(str(exc))

    def seconds_until_due(self) -> float:
        if self._dev is None:
            return IDLE_TICK_S
        polls = ["gpio"]
        if self._state in MONITORED:
            polls += ["adc", "temp"]
        due = min(self._next_poll[name] for name in polls) - self._clock()
        return min(max(0.0, due), self._gpio_period())

    def cancelled(self, what: str) -> None:
        self._event(Level.INFO, "cancelled",
                    f"{what} request cancelled by a later HV off or emergency stop")

    def internal_error(self, exc: BaseException) -> None:
        log.error("internal error", exc_info=exc)
        if self._dev is not None:
            self._enter_fault(f"internal error: {exc!r}")

    @property
    def state(self) -> State:
        return self._state

    def status(self) -> Status:
        now = self._clock()
        gpio = self._gpio
        power = None
        if self._kv is not None and self._ua is not None:
            power = self._kv * self._ua
        return Status(
            time=now,
            state=self._state,
            connected=self._dev is not None,
            serial=self._serial,
            unit=self._unit,
            interlock_closed=self._interlock_closed,
            interlock_epoch=self.interlock_epoch,
            controls_locked=self._controls_locked(now),
            enables_on=None if gpio is None else gpio.hv_enabled,
            tube_ready=None if gpio is None else gpio.tube_ready,
            monx_low_s=None if self._monx_low_since is None else now - self._monx_low_since,
            monx_drops=self._monx_drops,
            kv=self._kv,
            ua=self._ua,
            kv_average=self._avg_kv.value,
            ua_average=self._avg_ua.value,
            power_mw=power,
            power_average_mw=self._average_power(),
            band=None if self._band is None else self._band.band,
            range=self._range_status,
            temperature_c=self._temperature,
            setpoints=self._commitment,
            fault=self._fault,
        )

    # --- plans -----------------------------------------------------------------

    def _run_plan(self, steps: list[Step], *, abortable: bool) -> bool:
        """Execute steps; on any failure, leave HV off. Returns True if completed."""
        self._plan_depth += 1
        try:
            for step in steps:
                self._execute(step, abortable)
            return True
        except _Abort as exc:
            if exc.estop:
                self._emergency_stop()
            else:
                self._event(Level.WARNING, "aborted", str(exc))
                self._deenergize_now()
            return False
        except (_Fault, HvEnableError) as exc:
            self._enter_fault(str(exc))
            return False
        except _DEVICE_LOST as exc:
            self._lose_device(f"controller lost: {exc}")
            return False
        except DeviceError as exc:
            self._enter_fault(str(exc))
            return False
        finally:
            self._plan_depth -= 1

    def _execute(self, step: Step, abortable: bool) -> None:
        self._check_interrupts(abortable)
        dev = self._dev
        if isinstance(step, WriteDac):
            dev.write_dac(step.channel, step.counts)
            if step.channel == p.DAC_HV:
                self._dac = DacSetpoints(step.counts, self._dac.current_counts, self._unit.hv_factor)
            else:
                self._dac = DacSetpoints(self._dac.hv_counts, step.counts, self._unit.hv_factor)
        elif isinstance(step, SetEnable):
            try:
                gpio = dev.set_hv_enable(step.on)
            except InterlockOpenError as exc:
                raise _Abort(str(exc)) from exc
            self._handle_gpio(gpio)
            self._publish()
        elif isinstance(step, Wait):
            self._wait(step.seconds, abortable)
        elif isinstance(step, WaitForSettle):
            self._wait_for_settle(step, abortable)
        elif isinstance(step, RequireMonx):
            self._require_monx(step, abortable)
        elif isinstance(step, ConfigureTemperature):
            dev.configure_temperature_sensor()
        else:
            raise TypeError(f"unknown plan step {step!r}")

    def _wait(self, seconds: float, abortable: bool) -> None:
        """Sleep, polling GPIO each slice so the interlock stays watched."""
        deadline = self._clock() + seconds
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            self._sleep(min(remaining, self._gpio_period()))
            self._poll_gpio()
            self._publish()
            self._check_interrupts(abortable)

    def _wait_for_settle(self, step: WaitForSettle, abortable: bool) -> None:
        for _ in range(step.attempts):
            self._wait(step.interval_s, abortable)
            value = self._read_monitor(step.channel)
            self._publish()
            if value is not None and in_range(step.expected, value, step.tolerance):
                return
        name = "HV" if step.channel == p.ADC_HV else "current"
        units = "kV" if step.channel == p.ADC_HV else "µA"
        self._event(Level.WARNING, "settle_timeout",
                    f"{name} did not reach {step.expected:.2f} {units} within "
                    f"{step.attempts * step.interval_s:.1f} s; continuing")

    def _require_monx(self, step: RequireMonx, abortable: bool) -> None:
        deadline = self._clock() + step.timeout_s
        while True:
            if self._poll_gpio().tube_ready:
                return
            if self._clock() >= deadline:
                raise _Fault(f"the tube did not report ready (MONX) within {step.timeout_s:g} s")
            self._wait(min(self._gpio_period(), deadline - self._clock()), abortable)

    def _check_interrupts(self, abortable: bool) -> None:
        if self.estop.is_set():
            raise _Abort("emergency stop", estop=True)
        if not abortable:
            return
        if self.abort.is_set():
            self.abort.clear()
            raise _Abort("HV off requested")
        if self._interlock_closed is False:
            raise _Abort("interlock opened")

    def _deenergize_now(self) -> None:
        self._set_state(State.DEENERGIZING)
        if self._run_plan(plan_deenergize(self._unit), abortable=False):
            self._hv_switched(self._clock())
            self._set_state(State.IDLE)
            self._event(Level.INFO, "hv_off", "HV off")

    def _emergency_stop(self) -> None:
        self.estop.clear()
        self.abort.clear()
        if self._dev is None:
            return
        confirmed = self._dev.failsafe()
        self._dac = DacSetpoints.zero(self._unit)
        self._hv_switched(self._clock())
        if not self._dev.initialized:
            self._lose_device("controller lost: it stopped responding during an emergency stop")
            return
        if self._state is State.FAULT:
            self._event(Level.ERROR, "estop", "emergency stop: failsafe applied; fault remains")
            return
        if not confirmed:
            self._enter_fault("emergency stop could not confirm HV enables clear")
            return
        self._set_state(State.IDLE)
        self._event(Level.ERROR, "estop", "emergency stop: HV off")

    # --- polling ---------------------------------------------------------------

    def _poll_gpio(self) -> GpioState:
        gpio = self._dev.read_gpio()
        self._next_poll["gpio"] = self._clock() + self._gpio_period()
        self._handle_gpio(gpio)
        return gpio

    def _handle_gpio(self, gpio: GpioState) -> None:
        now = self._clock()
        self._gpio = gpio
        closed = gpio.interlock_closed
        if self._interlock_closed is None:
            if closed:
                self._clear_at = now + self._settings.safety.interlock_clear_s
        elif closed != self._interlock_closed:
            if closed:
                self._clear_at = now + self._settings.safety.interlock_clear_s
                self._event(Level.INFO, "interlock_closed",
                            "interlock closed; HV stays off until switched on again")
            else:
                self.interlock_epoch += 1
                self._event(Level.WARNING, "interlock_opened", "interlock opened")
        self._interlock_closed = closed

        if self._state is State.FAULT:
            return
        if gpio.hv_partial:
            raise _Fault(f"only one HV enable bit reads set (ADBUS {gpio.adbus:08b})")
        if self._plan_depth == 0:
            if self._state is State.IDLE and not gpio.hv_disabled:
                raise _Fault("HV enable bits read set while HV is off")
            if self._state is State.ON and not gpio.hv_enabled:
                raise _Fault("HV enable bits dropped while HV is on")
            if self._state is State.ON and closed:
                self._track_monx(now, gpio.tube_ready)

    def _poll_monitors(self) -> None:
        self._next_poll["adc"] = self._clock() + 1.0 / self._settings.polling.adc_hz
        kv = self._read_monitor(p.ADC_HV)
        ua = self._read_monitor(p.ADC_CURRENT)
        if kv is None or ua is None or self._plan_depth:
            return
        self._range_status = self._range.check(
            self._clock(), hv_on=self._state is State.ON, expected=self._expected, kv=kv, ua=ua)

    def _read_monitor(self, channel: int) -> float | None:
        try:
            reading = self._dev.read_adc(channel)
        except FramingError as exc:
            self._framing_errors += 1
            self._event(Level.WARNING, "framing", str(exc))
            if self._framing_errors >= MAX_FRAMING_ERRORS:
                raise _Fault(f"{self._framing_errors} ADC framing errors in a row") from exc
            return None
        self._framing_errors = 0
        if channel == p.ADC_HV:
            self._kv = reading.volts * self._unit.hv_factor
            self._avg_kv.add(self._kv)
            return self._kv
        self._ua = reading.volts * self._unit.current_factor
        self._avg_ua.add(self._ua)
        self._update_band()
        return self._ua

    def _average_power(self) -> float | None:
        kv, ua = self._avg_kv.value, self._avg_ua.value
        return None if kv is None or ua is None else kv * ua

    def _update_band(self) -> None:
        power = self._average_power()
        if power is not None:
            self._band.update(power)

    def _track_monx(self, now: float, ready: bool) -> None:
        if ready:
            if self._monx_warned:
                self._event(Level.INFO, "tube_ready", "the tube reports ready (MONX) again")
            self._monx_low_since = None
            self._monx_warned = False
        elif self._monx_low_since is None:
            self._monx_low_since = now
            self._monx_drops += 1
            self._monx_window_drops += 1
        elif (not self._monx_warned
              and now - self._monx_low_since >= self._settings.safety.monx_warning_s):
            self._monx_warned = True
            self._event(Level.WARNING, "tube_not_ready",
                        f"the tube has not reported ready (MONX) for "
                        f"{self._settings.safety.monx_warning_s:g} s with HV on")
        if now - self._monx_window_start >= MONX_SUMMARY_S:
            self._summarize_monx(now)

    def _summarize_monx(self, now: float) -> None:
        if self._monx_window_drops:
            self._event(Level.INFO, "monx_drops",
                        f"MONX dropped {self._monx_window_drops} times in the last "
                        f"{now - self._monx_window_start:.0f} s with HV on",
                        count=self._monx_window_drops)
        self._monx_window_drops = 0
        self._monx_window_start = now

    def _poll_temperature(self) -> None:
        self._next_poll["temp"] = self._clock() + 1.0 / self._settings.polling.temp_hz
        try:
            self._temperature = self._dev.read_temperature_c()
            self._temperature_problem = None
        except TemperatureNotConfigured as exc:
            self._temperature = None
            self._event(Level.WARNING, "temperature",
                        f"{exc}; reconfiguring the temperature sensor")
            self._dev.configure_temperature_sensor()
        except TemperatureError as exc:
            self._temperature = None
            if str(exc) != self._temperature_problem:
                self._temperature_problem = str(exc)
                self._event(Level.WARNING, "temperature", str(exc))

    # --- state helpers -------------------------------------------------------

    def _accept(self, kv: float, ua: float) -> Commitment | None:
        try:
            commitment = commit_setpoints(kv, ua, self._unit)
        except ValueError as exc:
            self._event(Level.WARNING, "refused", str(exc))
            return None
        for adjustment in commitment.adjustments:
            self._event(Level.WARNING, "adjusted", adjustment.message,
                        kind_name=adjustment.kind.name)
        self._commitment = commitment
        return commitment

    def _enter_fault(self, message: str) -> None:
        dev = self._dev
        confirmed = dev.failsafe() if dev is not None else True
        if self._unit is not None:
            self._dac = DacSetpoints.zero(self._unit)
        self._hv_switched(self._clock())
        if not confirmed:
            message += "; HV enables could not be confirmed clear: check the tube physically"
        self._fault = message
        self._set_state(State.FAULT)
        self._event(Level.ERROR, "fault", message)
        if dev is not None and not dev.initialized:
            self._lose_device("controller lost: it stopped responding")

    def _lose_device(self, message: str) -> None:
        confirmed = self._close_device() if self._dev is not None else True
        self._dev = None
        if not confirmed:
            message += "; HV enables could not be confirmed clear: check the tube physically"
        self._fault = message
        self._gpio = None
        self._interlock_closed = None      # unknown without a device
        self._set_state(State.FAULT)
        self._event(Level.ERROR, "device_lost", message)

    def _close_device(self) -> bool:
        try:
            return self._dev.close()
        except Exception as exc:        # close must never stop a shutdown
            log.error("closing the device failed: %s", exc)
            return False

    def _set_state(self, state: State) -> None:
        if state is not self._state:
            log.info("state: %s -> %s", self._state.value, state.value)
            self._state = state
        self._publish()

    def _hv_switched(self, now: float) -> None:
        # Readings from before the switch no longer describe the tube.
        if self._range is not None:
            self._range.hv_switched(now)
        self._range_status = None
        self._kv = self._ua = None
        self._reset_averages()
        self._summarize_monx(now)
        self._monx_drops = 0
        self._monx_low_since = None
        self._monx_warned = False

    def _reset_averages(self) -> None:
        self._avg_kv.reset()
        self._avg_ua.reset()
        if self._band is not None:
            self._band.reset()

    def _controls_locked(self, now: float) -> bool:
        return not self._interlock_closed or now < self._clear_at

    def _gpio_period(self) -> float:
        return 1.0 / self._settings.polling.gpio_hz

    def _event(self, level: Level, kind: str, message: str, **data: Any) -> None:
        event = Event(self._clock(), level, kind, message, data)
        log.log({Level.INFO: logging.INFO, Level.WARNING: logging.WARNING,
                 Level.ERROR: logging.ERROR}[level], "%s: %s", kind, message)
        _call_listener(self._listener.event, event)

    def _publish(self) -> None:
        _call_listener(self._listener.status, self.status())

    def _reset(self) -> None:
        self._state = State.DISCONNECTED
        self._dev: MiniX | None = None
        self._unit: UnitConfig | None = None
        self._serial: str | None = None
        self._gpio: GpioState | None = None
        self._interlock_closed: bool | None = None
        self._clear_at = math.inf
        self._dac: DacSetpoints | None = None
        self._commitment: Commitment | None = None
        self._expected: DacSetpoints | None = None
        self._kv: float | None = None
        self._ua: float | None = None
        self._avg_kv = RunningAverage()
        self._avg_ua = RunningAverage()
        self._range: RangeChecker | None = None
        self._range_status: RangeStatus | None = None
        self._band: PowerBandTracker | None = None
        self._monx_low_since: float | None = None
        self._monx_warned = False
        self._monx_drops = 0
        self._monx_window_drops = 0
        self._monx_window_start = 0.0
        self._temperature: float | None = None
        self._temperature_problem: str | None = None
        self._fault: str | None = None
        self._framing_errors = 0
        self._plan_depth = 0
        self._next_poll = {"gpio": 0.0, "adc": 0.0, "temp": 0.0}


_STOP = object()
PRIORITY_URGENT = 0
PRIORITY_NORMAL = 1


class Controller:
    """Runs a Session on a worker thread. All methods are thread-safe.

    deenergize() and emergency_stop() cancel any energize() or commit()
    still waiting in the queue, so a request never takes effect after a
    later request to switch HV off.
    """

    def __init__(self, settings: Settings, *, transport_factory: TransportFactory,
                 listener: Listener | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._estop = threading.Event()
        self._abort = threading.Event()
        self._session_args = dict(settings=settings, transport_factory=transport_factory,
                                  listener=listener, clock=clock, sleep=sleep,
                                  estop=self._estop, abort=self._abort)
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._thread = threading.Thread(target=self._run, name="minix-device", daemon=True)
        self._session: Session | None = None
        self._started = threading.Event()
        self._generation = 0
        self._generation_lock = threading.Lock()

    def start(self) -> None:
        self._thread.start()
        self._started.wait()

    def shutdown(self, timeout: float = 30.0) -> bool:
        """Disconnect (switching HV off first) and stop the worker. Returns False on timeout."""
        self._submit(PRIORITY_NORMAL, lambda s: s.disconnect())
        self._submit(PRIORITY_NORMAL, _STOP)
        self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def connect(self, serial: str) -> None:
        self._submit(PRIORITY_NORMAL, lambda s: s.connect(serial))

    def disconnect(self) -> None:
        self._submit(PRIORITY_NORMAL, lambda s: s.disconnect())

    def energize(self, kv: float, ua: float, interlock_epoch: int) -> None:
        self._submit(PRIORITY_NORMAL, self._cancellable(
            lambda s: s.energize(kv, ua, interlock_epoch), "HV on"))

    def commit(self, kv: float, ua: float) -> None:
        self._submit(PRIORITY_NORMAL, self._cancellable(lambda s: s.commit(kv, ua), "setpoints"))

    def deenergize(self) -> None:
        self._cancel_pending()
        self._abort.set()              # interrupts a running energize or commit
        self._submit(PRIORITY_URGENT, lambda s: s.deenergize())

    def emergency_stop(self) -> None:
        self._cancel_pending()
        self._estop.set()              # interrupts any running plan
        self._submit(PRIORITY_URGENT, lambda s: s.tick())

    def clear_fault(self) -> None:
        self._submit(PRIORITY_NORMAL, lambda s: s.clear_fault())

    def reload_settings(self, settings: Settings) -> None:
        self._submit(PRIORITY_NORMAL, lambda s: s.reload_settings(settings))

    def _submit(self, priority: int, item) -> None:
        self._queue.put((priority, next(self._seq), item))

    def _cancel_pending(self) -> None:
        with self._generation_lock:
            self._generation += 1

    def _cancellable(self, fn, what: str):
        with self._generation_lock:
            generation = self._generation

        def run(session: Session) -> None:
            with self._generation_lock:
                current = self._generation
            if generation != current:
                session.cancelled(what)
            else:
                fn(session)
        return run

    def _run(self) -> None:
        session = self._session = Session(**self._session_args)
        self._started.set()
        while True:
            try:
                _, _, item = self._queue.get(timeout=session.seconds_until_due())
            except queue.Empty:
                item = None
            if item is _STOP:
                return
            if item is not None:
                self._guarded(session, partial(item, session))
            self._guarded(session, session.tick)

    @staticmethod
    def _guarded(session: Session, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:
            try:
                session.internal_error(exc)
            except Exception:
                log.exception("failed to handle an internal error")
