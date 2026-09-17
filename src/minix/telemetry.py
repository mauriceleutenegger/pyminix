"""Run logging.

RunRecorder is a controller Listener that writes one run record per
connection, from "connected" to "disconnected" (or a lost device), as two
CSV files in the log directory:

    <start>_<serial>.csv          status samples at sample_hz, plus a row
                                  whenever the state changes. power_mw is
                                  the latest single reading; band follows
                                  power_average_mw. monx_drops counts MONX
                                  drops since HV was switched.
    <start>_<serial>_events.csv   every controller event

Both begin with '#' comment lines giving the serial number, the power
rating and where it came from, the board, and the software version
(§6.1.1). Read them with, e.g., pandas.read_csv(path, comment="#").

Rows are flushed as they are written. If writing fails, the recorder logs
the error, stops recording, and sets `error`; it never raises into the
controller.

setup_logging() configures the application log: a rotating file in the
same directory, plus the console.
"""

from __future__ import annotations

import csv
import logging
import logging.handlers
from collections.abc import Callable
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import IO

from .controller import Event, State, Status

log = logging.getLogger(__name__)

SAMPLE_COLUMNS = [
    "time", "elapsed_s", "state", "interlock_closed", "enables_on", "tube_ready",
    "monx_drops", "setpoint_kv", "setpoint_ua", "kv", "ua", "kv_average", "ua_average",
    "power_mw", "power_average_mw", "band", "in_range", "temperature_c", "fault",
]
EVENT_COLUMNS = ["time", "elapsed_s", "level", "kind", "message"]
RUN_ENDING_EVENTS = frozenset({"disconnected", "device_lost"})
APP_LOG_NAME = "minix.log"


def software_version() -> str:
    try:
        return f"minix {version('minix')}"
    except PackageNotFoundError:
        return "minix (not installed)"


class RunRecorder:
    def __init__(self, directory: Path, *, sample_hz: float = 1.0,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self._directory = Path(directory)
        self._interval = 1.0 / sample_hz
        self._wall_clock = wall_clock
        self._samples: _CsvFile | None = None
        self._events: _CsvFile | None = None
        self._start_time = 0.0
        self._last_sample_time = -float("inf")
        self._last_state: State | None = None
        self.error: str | None = None

    @property
    def recording(self) -> bool:
        return self._samples is not None

    @property
    def paths(self) -> tuple[Path, Path] | None:
        if self._samples is None:
            return None
        return self._samples.path, self._events.path

    # --- Listener --------------------------------------------------------------

    def status(self, status: Status) -> None:
        if self._samples is None:
            return
        due = status.time - self._last_sample_time >= self._interval - 1e-9
        if due or status.state is not self._last_state:
            self._last_sample_time = status.time
            self._last_state = status.state
            self._write(self._samples, self._sample_row(status))

    def event(self, event: Event) -> None:
        if event.kind == "connected":
            self._start(event)
        if self._events is None:
            return
        self._write(self._events, [
            self._timestamp(), _num(event.time - self._start_time, 3),
            event.level.value, event.kind, event.message,
        ])
        if event.kind in RUN_ENDING_EVENTS:
            self.close()

    # --- run files -------------------------------------------------------------

    def close(self) -> None:
        for f in (self._samples, self._events):
            if f is not None:
                f.close()
        self._samples = self._events = None
        self._last_state = None

    def _start(self, event: Event) -> None:
        self.close()
        self.error = None
        serial = event.data.get("serial", "unknown")
        started = self._wall_clock()
        stem = f"{started:%Y%m%d-%H%M%S}_{serial}"
        header = [
            "minix run record",
            f"started: {started.isoformat(timespec='seconds')}",
            f"serial: {serial}",
            f"unit: {event.message}",
            f"device type: {event.data.get('device_type', 'unknown')}",
            f"power rating W: {event.data.get('watt_max_w', 'unknown')}",
            f"rating source: {event.data.get('rating_source') or 'not recorded'}",
            f"safety margin W: {event.data.get('safety_margin_w', 'unknown')}",
            f"software: {software_version()}",
        ]
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._samples = _CsvFile.create(self._unique(stem, ".csv"), header, SAMPLE_COLUMNS)
            self._events = _CsvFile.create(self._unique(stem, "_events.csv"), header,
                                           EVENT_COLUMNS)
        except OSError as exc:
            self._fail(exc)
            return
        self._start_time = event.time
        self._last_sample_time = -float("inf")
        log.info("recording run to %s", self._samples.path)

    def _unique(self, stem: str, suffix: str) -> Path:
        path = self._directory / f"{stem}{suffix}"
        n = 1
        while path.exists():
            path = self._directory / f"{stem}-{n}{suffix}"
            n += 1
        return path

    def _sample_row(self, s: Status) -> list[str]:
        sp = s.setpoints
        return [
            self._timestamp(), _num(s.time - self._start_time, 3), s.state.name,
            _flag(s.interlock_closed), _flag(s.enables_on), _flag(s.tube_ready),
            str(s.monx_drops), _num(sp and sp.kv, 4), _num(sp and sp.ua, 3),
            _num(s.kv, 4), _num(s.ua, 3), _num(s.kv_average, 4), _num(s.ua_average, 3),
            _num(s.power_mw, 1), _num(s.power_average_mw, 1), s.band.name if s.band else "",
            _flag(s.range.ok) if s.range and s.range.testing else "",
            _num(s.temperature_c, 4), s.fault or "",
        ]

    def _timestamp(self) -> str:
        return self._wall_clock().isoformat(timespec="milliseconds")

    def _write(self, f: _CsvFile, row: list[str]) -> None:
        try:
            f.write(row)
        except (OSError, ValueError) as exc:     # ValueError: the file was closed
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        self.error = f"run recording stopped: {exc}"
        log.error(self.error)
        try:
            self.close()
        except OSError:
            self._samples = self._events = None


class _CsvFile:
    def __init__(self, path: Path, handle: IO[str]):
        self.path = path
        self._handle = handle
        self._writer = csv.writer(handle)

    @classmethod
    def create(cls, path: Path, header: list[str], columns: list[str]) -> _CsvFile:
        handle = open(path, "x", newline="", encoding="utf-8")
        f = cls(path, handle)
        for line in header:
            handle.write(f"# {line}\n")
        f.write(columns)
        return f

    def write(self, row: list[str]) -> None:
        self._writer.writerow(row)
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def _num(value: float | None, decimals: int) -> str:
    return "" if value is None else f"{value:.{decimals}f}"


def _flag(value: bool | None) -> str:
    return "" if value is None else str(int(value))


def setup_logging(directory: Path, level: int = logging.INFO) -> Path:
    """Log to a rotating file in directory and to the console. Returns the file path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / APP_LOG_NAME
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_minix", False):
            root.removeHandler(handler)
            handler.close()
    for handler in (file_handler, console):
        handler._minix = True
        root.addHandler(handler)
    logging.captureWarnings(True)
    return path
