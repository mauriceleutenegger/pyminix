"""Main window: connection, setpoints, monitors, status lamps, HV controls.

The window never talks to the device. It sends requests to the Controller
and shows the Status snapshots and Events that arrive through QtBridge.
Its checks (spin-box ranges, enabled buttons, the power preview) are for
the operator's benefit; the controller and policy layer enforce the limits
regardless.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..config import Settings, UnitConfig
from ..controller import HV_ACTIVE, Controller, Event, Level, State, Status
from ..discovery import ControllerInfo
from ..policy.limits import AdjustmentKind, commit_setpoints
from ..sim import SimFaults, SimHarness
from .bridge import QtBridge
from .dialogs import Confirmer, ask_rating
from .widgets import AMBER, GREY, RED, Banner, Lamp, PowerBar, XrayIndicator

DEFAULT_KV = 15.0          # the reference's defaults (§6.1)
DEFAULT_UA = 15.0
WATCHDOG_S = 3.0
LOG_LINES = 2000

LEVEL_COLOURS = {Level.INFO: "black", Level.WARNING: AMBER, Level.ERROR: RED}


class MainWindow(QMainWindow):
    def __init__(
        self,
        controller: Controller,
        bridge: QtBridge,
        settings: Settings,
        *,
        discover: Callable[[], list[ControllerInfo]],
        save_rating: Callable[[str, float, str], Settings],
        confirmer: Confirmer | None = None,
        ask_rating: Callable[[QWidget, str], tuple[float, str] | None] = ask_rating,
        sim: SimHarness | None = None,
        recording: Callable[[], str] | None = None,
        title_suffix: str = "",
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__()
        self._controller = controller
        self._settings = settings
        self._discover = discover
        self._save_rating = save_rating
        self._confirmer = confirmer or Confirmer()
        self._ask_rating = ask_rating
        self._sim = sim
        self._recording = recording
        self._clock = clock
        self._title_suffix = title_suffix
        self._status: Status | None = None
        self._status_received_at = clock()
        self._last_display = -float("inf")
        self._unit: UnitConfig | None = None

        self._set_title("Mini-X controller")
        self._build()
        bridge.status_received.connect(self._on_status)
        bridge.event_received.connect(self._on_event)

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(500)
        self._watchdog.timeout.connect(self._check_watchdog)
        self._watchdog.start()

        self.refresh_devices()
        self._apply(Status(time=clock(), state=State.DISCONNECTED))

    # --- layout ----------------------------------------------------------------

    def _build(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)

        # connection
        row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(220)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_devices)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._on_connect)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self._controller.disconnect)
        for w in (QLabel("Controller:"), self.device_combo, self.refresh_button,
                  self.connect_button, self.disconnect_button):
            row.addWidget(w)
        row.addStretch()
        root.addLayout(row)

        self.unit_label = QLabel("Not connected")
        self.unit_label.setStyleSheet("font-size:13pt; font-weight:bold;")
        self.state_label = QLabel()
        row = QHBoxLayout()
        row.addWidget(self.unit_label)
        row.addStretch()
        row.addWidget(self.state_label)
        root.addLayout(row)

        self.watchdog_banner = Banner()
        self.fault_banner = Banner()
        self.clear_fault_button = QPushButton("Clear fault")
        self.clear_fault_button.clicked.connect(self._controller.clear_fault)
        self.clear_fault_button.hide()
        root.addWidget(self.watchdog_banner)
        row = QHBoxLayout()
        row.addWidget(self.fault_banner, 1)
        row.addWidget(self.clear_fault_button)
        root.addLayout(row)

        self.xray = XrayIndicator()
        root.addWidget(self.xray)

        panels = QHBoxLayout()
        panels.addWidget(self._build_setpoints(), 1)
        panels.addWidget(self._build_monitors(), 1)
        root.addLayout(panels)

        lamps = QHBoxLayout()
        self.interlock_lamp = Lamp("Interlock")
        self.enables_lamp = Lamp("HV enables")
        self.ready_lamp = Lamp("Tube ready")
        for lamp in (self.interlock_lamp, self.enables_lamp, self.ready_lamp):
            lamps.addWidget(lamp)
        lamps.addStretch()
        root.addLayout(lamps)

        buttons = QHBoxLayout()
        self.hv_on_button = QPushButton("HV ON")
        self.hv_on_button.clicked.connect(self._on_hv_on)
        self.hv_off_button = QPushButton("HV OFF")
        self.hv_off_button.clicked.connect(self._controller.deenergize)
        self.estop_button = QPushButton("EMERGENCY STOP")
        # A border makes Qt draw the button from the stylesheet alone, so it
        # stays solid red on every platform style.
        self.estop_button.setStyleSheet(
            f"QPushButton {{ background:{RED}; color:white; font-size:14pt; font-weight:bold;"
            " border:2px solid #8e0000; border-radius:6px; padding:8px; }"
            "QPushButton:pressed { background:#8e0000; }")
        self.estop_button.setMinimumHeight(40)
        self.estop_button.clicked.connect(self._controller.emergency_stop)
        for b in (self.hv_on_button, self.hv_off_button):
            b.setMinimumHeight(40)
            b.setStyleSheet("font-size:13pt; font-weight:bold;")
            buttons.addWidget(b)
        buttons.addWidget(self.estop_button, 1)
        root.addLayout(buttons)

        if self._sim is not None:
            root.addWidget(self._build_sim_panel())

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_LINES)
        self.log_view.setMinimumHeight(120)
        root.addWidget(self.log_view, 1)

        self.recording_label = QLabel()
        self.statusBar().addPermanentWidget(self.recording_label)
        self.setCentralWidget(central)

    def _build_setpoints(self) -> QGroupBox:
        box = QGroupBox("Setpoints")
        form = QFormLayout(box)
        self.kv_spin = QDoubleSpinBox()
        self.kv_spin.setDecimals(2)
        self.kv_spin.setSingleStep(0.5)
        self.kv_spin.setSuffix(" kV")
        self.ua_spin = QDoubleSpinBox()
        self.ua_spin.setDecimals(2)
        self.ua_spin.setSingleStep(1.0)
        self.ua_spin.setSuffix(" µA")
        for spin in (self.kv_spin, self.ua_spin):
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(self._update_preview)
        self.kv_range = QLabel()
        self.ua_range = QLabel()
        form.addRow("High voltage", self.kv_spin)
        form.addRow("", self.kv_range)
        form.addRow("Emission current", self.ua_spin)
        form.addRow("", self.ua_range)
        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        form.addRow(self.preview_label)
        self.update_button = QPushButton("Update")
        self.update_button.clicked.connect(self._on_update)
        form.addRow(self.update_button)
        self.committed_label = QLabel("Committed: —")
        self.committed_label.setWordWrap(True)
        form.addRow(self.committed_label)
        return box

    def _build_monitors(self) -> QGroupBox:
        box = QGroupBox("Monitors")
        grid = QGridLayout(box)
        big = "font-size:20pt; font-weight:bold;"
        self.kv_value = QLabel("—")
        self.ua_value = QLabel("—")
        for label in (self.kv_value, self.ua_value):
            label.setStyleSheet(big)
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(QLabel("High voltage"), 0, 0)
        grid.addWidget(self.kv_value, 0, 1)
        grid.addWidget(QLabel("Emission current"), 1, 0)
        grid.addWidget(self.ua_value, 1, 1)
        grid.addWidget(QLabel("Power"), 2, 0)
        self.power_bar = PowerBar()
        grid.addWidget(self.power_bar, 2, 1)
        self.limit_label = QLabel()
        grid.addWidget(self.limit_label, 3, 1)
        grid.addWidget(QLabel("Board temperature"), 4, 0)
        self.temperature_value = QLabel("—")
        grid.addWidget(self.temperature_value, 4, 1)
        self.range_label = QLabel()
        self.range_label.setWordWrap(True)
        grid.addWidget(self.range_label, 5, 0, 1, 2)
        return box

    def _build_sim_panel(self) -> QGroupBox:
        box = QGroupBox("Simulator (settings also apply to the next connection)")
        row = QHBoxLayout(box)
        self.sim_controls = {}
        for name in SimFaults.names():
            check = QCheckBox(SimFaults.LABELS[name])
            check.setChecked(getattr(self._sim.faults, name))
            check.toggled.connect(lambda on, name=name: self._sim.set_fault(name, on))
            row.addWidget(check)
            self.sim_controls[name] = check
        row.addStretch()
        return box

    # --- actions ---------------------------------------------------------------

    def refresh_devices(self) -> None:
        current = self.device_combo.currentData()
        self.device_combo.clear()
        try:
            devices = self._discover()
        except OSError as exc:
            self._log(Level.ERROR, f"cannot list controllers: {exc}")
            devices = []
        for info in devices:
            self.device_combo.addItem(f"{info.serial}  ({info.description})", info.serial)
        if not devices:
            self.device_combo.addItem("no controller found", None)
        index = self.device_combo.findData(current)
        if index >= 0:
            self.device_combo.setCurrentIndex(index)
        self._update_controls()

    def _on_connect(self) -> None:
        serial = self.device_combo.currentData()
        if serial:
            self._controller.connect(serial)

    def _on_update(self) -> None:
        if self._status is not None and self._status.can_commit:
            self._controller.commit(self.kv_spin.value(), self.ua_spin.value())

    def _on_hv_on(self) -> None:
        status = self._status
        if status is None or not status.can_energize or self._watchdog_tripped():
            return
        kv, ua = self.kv_spin.value(), self.ua_spin.value()
        epoch = status.interlock_epoch
        preview = commit_setpoints(kv, ua, status.unit)
        text = (f"Switch the X-ray high voltage on?\n\n"
                f"{preview.kv:.2f} kV, {preview.ua:.2f} µA ({preview.power_mw / 1000:.2f} W)")
        if preview.adjustments:
            text += "\n\n" + "\n".join(a.message for a in preview.adjustments)
        if not self._confirmer.confirm(self, "Switch HV on", text):
            return
        latest = self._status
        if latest is None or latest.interlock_epoch != epoch or not latest.can_energize:
            self._log(Level.WARNING, "HV on cancelled: the interlock or state changed "
                                     "while the question was open")
            return
        self._controller.energize(kv, ua, epoch)

    def closeEvent(self, event: QCloseEvent) -> None:
        status = self._status
        hv_may_be_on = status is not None and (status.state in HV_ACTIVE or status.enables_on)
        if hv_may_be_on and not self._confirmer.confirm(
                self, "Quit", "HV is on. Switch it off and quit?"):
            event.ignore()
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._controller.shutdown()
        finally:
            QApplication.restoreOverrideCursor()
        self._watchdog.stop()
        event.accept()

    # --- controller updates ----------------------------------------------------

    def _on_status(self, status: Status) -> None:
        self._status_received_at = self._clock()
        previous = self._status
        self._status = status
        if self._confirmer.open and previous is not None and (
                status.interlock_epoch != previous.interlock_epoch
                or not status.can_energize):
            self._confirmer.revoke()
        self._apply(status)

    def _on_event(self, event: Event) -> None:
        self._log(event.level, event.message)
        if event.kind == "interlock_opened":
            self._confirmer.revoke()
        elif event.kind == "rating_required":
            self._request_rating(event.data["serial"])
        elif event.kind in ("adjusted", "refused") and event.level is not Level.INFO:
            self.statusBar().showMessage(event.message, 10000)

    def _request_rating(self, serial: str) -> None:
        answer = self._ask_rating(self, serial)
        if answer is None:
            self._log(Level.WARNING, f"not connected: no power rating for {serial}")
            return
        watts, source = answer
        try:
            self._settings = self._save_rating(serial, watts, source)
        except (OSError, ValueError) as exc:
            self._log(Level.ERROR, f"could not save the rating: {exc}")
            return
        self._log(Level.INFO, f"saved a {watts:g} W rating for {serial}")
        self._controller.reload_settings(self._settings)
        self._controller.connect(serial)

    def _apply(self, status: Status) -> None:
        unit = status.unit if status.connected else None
        if unit != self._unit:
            self._set_unit(unit)
        self.state_label.setText(f"State: <b>{status.state.value}</b>")

        if status.fault:
            self.fault_banner.show_message("error", f"FAULT: {status.fault}")
            self.clear_fault_button.show()
        else:
            self.fault_banner.clear()
            self.clear_fault_button.hide()

        self.xray.set_on(status.state in HV_ACTIVE or bool(status.enables_on))
        self._apply_lamps(status)

        now = self._clock()
        display_period = 1.0 / self._settings.polling.display_hz
        if (now - self._last_display >= display_period
                or self._status is None or status.state is not self._displayed_state):
            self._last_display = now
            self._displayed_state = status.state
            self._apply_monitors(status)

        sp = status.setpoints
        if sp is None:
            self.committed_label.setText("Committed: —")
        else:
            where = "applied" if status.state is State.ON else "applied when HV is switched on"
            self.committed_label.setText(
                f"Committed: {sp.kv:.2f} kV, {sp.ua:.2f} µA ({sp.power_mw / 1000:.2f} W), {where}")
        if self._recording is not None:
            self.recording_label.setText(self._recording())
        self._update_controls()

    _displayed_state: State | None = None

    def _apply_lamps(self, status: Status) -> None:
        if status.interlock_closed is None:
            self.interlock_lamp.set("off", "Interlock")
        elif not status.interlock_closed:
            self.interlock_lamp.set("alarm", "Interlock OPEN")
        elif status.controls_locked:
            self.interlock_lamp.set("warn", "Interlock restored")
        else:
            self.interlock_lamp.set("ok", "Interlock closed")
        if status.enables_on:
            self.enables_lamp.set("alarm", "HV enabled")
        else:
            self.enables_lamp.set("off", "HV enables off")
        if status.state is State.ON:
            # Brief MONX drops are common near 200 µA (§10.8): count them,
            # and only show "not ready" once MONX has stayed low a while.
            low = status.monx_low_s
            if low is not None and low >= self._settings.safety.monx_warning_s:
                self.ready_lamp.set("warn", "Tube not ready")
            elif status.monx_drops:
                self.ready_lamp.set("ok", f"Tube ready ({status.monx_drops} brief drops)")
            else:
                self.ready_lamp.set("ok", "Tube ready")
        elif status.tube_ready:
            self.ready_lamp.set("ok", "Tube ready")
        else:
            self.ready_lamp.set("off", "Tube not ready")

    def _apply_monitors(self, status: Status) -> None:
        kv = status.kv_average if status.kv_average is not None else status.kv
        ua = status.ua_average if status.ua_average is not None else status.ua
        self.kv_value.setText("—" if kv is None else f"{kv:.2f} kV")
        self.ua_value.setText("—" if ua is None else f"{ua:.2f} µA")
        power = status.power_average_mw
        if power is None or status.band is None or self._unit is None:
            self.power_bar.clear()
        else:
            self.power_bar.set_power(power, status.band, self._unit.rated_mw)
        self.temperature_value.setText(
            "—" if status.temperature_c is None else f"{status.temperature_c:.1f} °C")
        r = status.range
        if r is None or not r.testing:
            self.range_label.setText("")
        elif r.ok:
            self.range_label.setText('<span style="color:#2e7d32">Monitors in range</span>')
        else:
            parts = [name for name, ok in (("HV", r.hv_ok), ("current", r.current_ok),
                                           ("power", r.power_ok)) if not ok]
            self.range_label.setText(
                f'<span style="color:{AMBER}; font-weight:bold">OUT OF RANGE: '
                f'{", ".join(parts)}</span>')

    def _set_unit(self, unit: UnitConfig | None) -> None:
        self._unit = unit
        if unit is None:
            self.unit_label.setText("Not connected")
            self._set_title("Mini-X controller")
            self.limit_label.setText("")
            self.kv_range.setText("")
            self.ua_range.setText("")
            self._update_preview()
            return
        text = unit.describe()
        self.unit_label.setText(text)
        self._set_title(f"Mini-X controller — {text}")
        self.limit_label.setText(f"rating {unit.watt_max_w:g} W, "
                                 f"setpoint limit {unit.safe_mw / 1000:.2f} W")
        for spin, low, high, default in ((self.kv_spin, unit.hv_min_kv, unit.hv_max_kv, DEFAULT_KV),
                                         (self.ua_spin, unit.current_min_ua,
                                          unit.current_max_ua, DEFAULT_UA)):
            keep = spin.value() if low <= spin.value() <= high else default
            spin.setRange(low, high)
            spin.setValue(keep)
        self.kv_range.setText(f"{unit.hv_min_kv:g} – {unit.hv_max_kv:g} kV")
        self.ua_range.setText(f"{unit.current_min_ua:g} – {unit.current_max_ua:g} µA")
        self._update_preview()

    def _set_title(self, title: str) -> None:
        self.setWindowTitle(title + self._title_suffix)

    def _update_preview(self) -> None:
        if self._unit is None:
            self.preview_label.setText("")
            return
        c = commit_setpoints(self.kv_spin.value(), self.ua_spin.value(), self._unit)
        reduced = [a for a in c.adjustments if a.kind is AdjustmentKind.CURRENT_REDUCED_FOR_POWER]
        text = f"{c.kv:.2f} kV × {c.ua:.2f} µA = {c.power_mw / 1000:.2f} W"
        if reduced:
            self.preview_label.setText(
                f'<span style="color:{AMBER}; font-weight:bold">Current will be reduced to '
                f'{c.ua:.2f} µA to stay under {self._unit.safe_mw / 1000:.2f} W.</span><br>{text}')
        else:
            self.preview_label.setText(f'<span style="color:{GREY}">{text}</span>')

    def _update_controls(self) -> None:
        status = self._status
        connected = status is not None and status.connected
        state = status.state if status else State.DISCONNECTED
        responsive = not self._watchdog_tripped()
        self.connect_button.setEnabled(
            state is State.DISCONNECTED and bool(self.device_combo.currentData()))
        self.device_combo.setEnabled(state is State.DISCONNECTED)
        self.refresh_button.setEnabled(state is State.DISCONNECTED)
        self.disconnect_button.setEnabled(connected or state is State.FAULT)
        self.kv_spin.setEnabled(connected)
        self.ua_spin.setEnabled(connected)
        self.update_button.setEnabled(bool(status and status.can_commit) and responsive)
        self.hv_on_button.setEnabled(bool(status and status.can_energize) and responsive)
        self.hv_off_button.setEnabled(state in HV_ACTIVE or bool(status and status.enables_on))
        self.estop_button.setEnabled(True)
        self.update_button.setText("Update" if state is State.ON else "Update (applies at HV on)")

    # --- watchdog and log ------------------------------------------------------

    def _watchdog_tripped(self) -> bool:
        status = self._status
        if not self._controller.alive:
            return True
        if status is None or not status.connected:
            return False
        return self._clock() - self._status_received_at > WATCHDOG_S

    def _check_watchdog(self) -> None:
        if not self._controller.alive:
            self.watchdog_banner.show_message(
                "error", "The controller has stopped. Check the tube and restart the program.")
        elif self._watchdog_tripped():
            self.watchdog_banner.show_message(
                "error", f"No update from the controller for over {WATCHDOG_S:g} s. "
                         "Use EMERGENCY STOP if HV may be on, and check the tube.")
        else:
            self.watchdog_banner.clear()
        self._update_controls()

    def _log(self, level: Level, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        colour = LEVEL_COLOURS[level]
        safe = (message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        self.log_view.appendHtml(f'<span style="color:{colour}">{stamp}  {safe}</span>')
