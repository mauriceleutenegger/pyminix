"""The main window, with a real controller thread and the simulator."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from minix.config import PollingConfig, SafetyConfig, Settings, UnitEntry
from minix.controller import Controller, State
from minix.discovery import ControllerInfo
from minix.sim import SimHarness
from minix.ui import app as app_module
from minix.ui.bridge import QtBridge
from minix.ui.dialogs import Confirmer, RatingDialog
from minix.ui.main_window import MainWindow
from minix.ui.widgets import Lamp

SERIAL = "01300036"
WAIT = 10_000


class FakeConfirmer:
    def __init__(self, answer=True):
        self.answer = answer
        self.questions = []
        self.revoked = False
        self.open = False
        self.while_open = None

    def confirm(self, parent, title, text):
        self.questions.append((title, text))
        self.open, self.revoked = True, False
        try:
            if self.while_open:
                self.while_open()
        finally:
            self.open = False
        return self.answer and not self.revoked

    def revoke(self):
        if self.open:
            self.revoked = True


def fast_settings(units=None):
    return Settings(units={SERIAL: UnitEntry(10.0, "test")} if units is None else units,
                    safety=SafetyConfig(interlock_clear_s=0.2),
                    polling=PollingConfig(gpio_hz=50, adc_hz=10, temp_hz=2, display_hz=20))


def make_gui(qtbot, settings, ask_rating=None):
    sims = []
    harness = SimHarness(settle_tau_s=0.02, monx_delay_s=0.01)

    def factory(serial):
        sims.append(harness.create(serial))
        return sims[-1]

    saved = []

    def save_rating(serial, watts, source):
        saved.append((serial, watts, source))
        return Settings(units={serial: UnitEntry(watts, source)}, safety=settings.safety,
                        polling=settings.polling)

    bridge = QtBridge()
    controller = Controller(settings, transport_factory=factory, listener=bridge)
    controller.start()
    confirmer = FakeConfirmer()
    kwargs = {} if ask_rating is None else {"ask_rating": ask_rating}
    window = MainWindow(
        controller, bridge, settings,
        discover=lambda: [ControllerInfo(SERIAL, "MiniX Controller", None, None)],
        save_rating=save_rating, confirmer=confirmer,
        sim=harness, **kwargs)
    qtbot.addWidget(window)
    window.show()
    return SimpleNamespace(window=window, controller=controller, sims=sims,
                           harness=harness, confirmer=confirmer, saved=saved)


@pytest.fixture
def gui(qtbot):
    g = make_gui(qtbot, fast_settings())
    yield g
    g.controller.shutdown()


def state(g):
    return g.window._status.state if g.window._status else None


def connect(qtbot, g):
    qtbot.mouseClick(g.window.connect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: g.window.hv_on_button.isEnabled(), timeout=WAIT)


def switch_on(qtbot, g):
    qtbot.mouseClick(g.window.hv_on_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: state(g) is State.ON, timeout=WAIT)


def log_text(g):
    return g.window.log_view.toPlainText()


def test_initial_state(gui):
    w = gui.window
    assert w.unit_label.text() == "Not connected"
    assert w.connect_button.isEnabled()
    assert not w.hv_on_button.isEnabled()
    assert not w.hv_off_button.isEnabled()
    assert not w.update_button.isEnabled()
    assert w.estop_button.isEnabled()
    assert not w.xray.on
    assert w.device_combo.currentData() == SERIAL


def test_connect(qtbot, gui):
    connect(qtbot, gui)
    w = gui.window
    assert w.unit_label.text() == "Mini-X 01300036: 50 kV, NSI, 10 W rating"
    assert "10 W rating" in w.windowTitle()
    assert "setpoint limit 9.95 W" in w.limit_label.text()
    assert (w.kv_spin.minimum(), w.kv_spin.maximum()) == (10.0, 50.0)
    assert (w.ua_spin.minimum(), w.ua_spin.maximum()) == (5.0, 200.0)
    assert (w.kv_spin.value(), w.ua_spin.value()) == (15.0, 15.0)
    assert w.interlock_lamp.kind == "ok"
    assert not w.connect_button.isEnabled() and w.disconnect_button.isEnabled()
    qtbot.waitUntil(lambda: w.temperature_value.text() != "—", timeout=WAIT)


def test_hv_on_and_off(qtbot, gui):
    connect(qtbot, gui)
    w = gui.window
    w.kv_spin.setValue(20)
    w.ua_spin.setValue(50)
    switch_on(qtbot, gui)
    title, text = gui.confirmer.questions[-1]
    assert "20.00 kV, 50.00 µA (1.00 W)" in text
    assert gui.sims[-1].supply_on
    assert w.xray.on and w.enables_lamp.kind == "alarm"
    qtbot.waitUntil(lambda: w.kv_value.text().startswith("20."), timeout=WAIT)
    qtbot.waitUntil(lambda: w.ready_lamp.kind == "ok", timeout=WAIT)
    assert "applied" in w.committed_label.text()
    assert w.hv_off_button.isEnabled() and not w.hv_on_button.isEnabled()

    qtbot.mouseClick(w.hv_off_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: state(gui) is State.IDLE, timeout=WAIT)
    assert not gui.sims[-1].hv_enabled
    assert not w.xray.on


def test_declined_confirmation_does_nothing(qtbot, gui):
    connect(qtbot, gui)
    gui.confirmer.answer = False
    qtbot.mouseClick(gui.window.hv_on_button, Qt.MouseButton.LeftButton)
    qtbot.wait(300)
    assert state(gui) is State.IDLE
    assert not gui.sims[-1].hv_enabled


def test_interlock_opening_withdraws_the_question(qtbot, gui):
    connect(qtbot, gui)
    sim = gui.sims[-1]

    def open_interlock():
        sim.set_interlock(False)
        qtbot.waitUntil(lambda: gui.confirmer.revoked, timeout=WAIT)
        sim.set_interlock(True)

    gui.confirmer.while_open = open_interlock
    qtbot.mouseClick(gui.window.hv_on_button, Qt.MouseButton.LeftButton)
    qtbot.wait(500)
    assert state(gui) is State.IDLE
    assert not sim.hv_enabled
    assert "interlock opened" in log_text(gui)


def test_update_while_on(qtbot, gui):
    connect(qtbot, gui)
    switch_on(qtbot, gui)
    w = gui.window
    w.kv_spin.setValue(30)
    w.ua_spin.setValue(100)
    qtbot.mouseClick(w.update_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "30.00 kV, 100.00 µA" in w.committed_label.text(), timeout=WAIT)
    qtbot.waitUntil(lambda: gui.sims[-1].hv_setpoint_kv == pytest.approx(30), timeout=WAIT)


def test_update_while_off_is_stored(qtbot, gui):
    connect(qtbot, gui)
    w = gui.window
    assert w.update_button.text() == "Update (applies at HV on)"
    w.kv_spin.setValue(25)
    qtbot.mouseClick(w.update_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "applied when HV is switched on" in w.committed_label.text(),
                    timeout=WAIT)
    assert gui.sims[-1].hv_dac == 0


def test_power_preview(qtbot, gui):
    connect(qtbot, gui)
    w = gui.window
    w.kv_spin.setValue(20)
    w.ua_spin.setValue(10)
    assert "20.00 kV × 10.00 µA = 0.20 W" in w.preview_label.text()
    w.kv_spin.setValue(50)
    w.ua_spin.setValue(200)
    assert "reduced to 198.95 µA" in w.preview_label.text()


def test_adjustment_is_reported(qtbot, gui):
    connect(qtbot, gui)
    w = gui.window
    w.kv_spin.setValue(50)
    w.ua_spin.setValue(200)
    switch_on(qtbot, gui)
    assert "reduced" in gui.confirmer.questions[-1][1]
    qtbot.waitUntil(lambda: "power limit" in log_text(gui), timeout=WAIT)
    assert gui.sims[-1].max_commanded_mw < 9950


def test_emergency_stop(qtbot, gui):
    connect(qtbot, gui)
    switch_on(qtbot, gui)
    qtbot.mouseClick(gui.window.estop_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "emergency stop" in log_text(gui), timeout=WAIT)
    assert not gui.sims[-1].hv_enabled
    qtbot.waitUntil(lambda: not gui.window.xray.on, timeout=WAIT)


def test_interlock_opening_while_on(qtbot, gui):
    connect(qtbot, gui)
    switch_on(qtbot, gui)
    w = gui.window
    w.sim_controls["interlock_closed"].setChecked(False)
    qtbot.waitUntil(lambda: state(gui) is State.IDLE, timeout=WAIT)
    assert w.interlock_lamp.kind == "alarm"
    assert not w.hv_on_button.isEnabled()
    w.sim_controls["interlock_closed"].setChecked(True)
    qtbot.waitUntil(lambda: w.interlock_lamp.kind == "warn", timeout=WAIT)
    qtbot.waitUntil(lambda: w.hv_on_button.isEnabled(), timeout=WAIT)
    assert not gui.sims[-1].hv_enabled


def test_fault_and_clear(qtbot, gui):
    connect(qtbot, gui)
    w = gui.window
    w.sim_controls["tube_never_ready"].setChecked(True)
    qtbot.mouseClick(w.hv_on_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: state(gui) is State.FAULT, timeout=WAIT)
    assert w.fault_banner.isVisible() and "MONX" in w.fault_banner.text()
    assert w.clear_fault_button.isVisible()
    assert not gui.sims[-1].hv_enabled
    w.sim_controls["tube_never_ready"].setChecked(False)
    qtbot.mouseClick(w.clear_fault_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: state(gui) is State.IDLE, timeout=WAIT)
    assert not w.fault_banner.isVisible()


def test_lost_device(qtbot, gui):
    connect(qtbot, gui)
    gui.window.sim_controls["usb_failure"].setChecked(True)
    qtbot.waitUntil(lambda: state(gui) is State.FAULT, timeout=WAIT)
    assert gui.window.unit_label.text() == "Not connected"
    qtbot.mouseClick(gui.window.clear_fault_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: gui.window.connect_button.isEnabled(), timeout=WAIT)


def test_sim_settings_apply_to_the_next_connection(qtbot, gui):
    # The bug found in manual testing: a box ticked while disconnected
    # showed its state but the new simulator started with defaults.
    w = gui.window
    w.sim_controls["interlock_closed"].setChecked(False)
    w.sim_controls["tube_never_ready"].setChecked(True)
    qtbot.mouseClick(w.connect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: w.interlock_lamp.kind == "alarm", timeout=WAIT)
    sim = gui.sims[-1]
    assert not sim.interlock_closed and sim.monx_delay_s == 1e9
    assert not w.hv_on_button.isEnabled()


def test_sim_settings_survive_a_reconnect(qtbot, gui):
    connect(qtbot, gui)
    w = gui.window
    w.sim_controls["tube_never_ready"].setChecked(True)
    qtbot.mouseClick(w.disconnect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: w.connect_button.isEnabled(), timeout=WAIT)
    connect(qtbot, gui)
    assert len(gui.sims) == 2 and gui.sims[-1].monx_delay_s == 1e9
    qtbot.mouseClick(w.hv_on_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: state(gui) is State.FAULT, timeout=WAIT)


def test_usb_failure_before_connecting(qtbot, gui):
    w = gui.window
    w.sim_controls["usb_failure"].setChecked(True)
    qtbot.mouseClick(w.connect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: state(gui) is State.FAULT, timeout=WAIT)
    assert "connection failed" in w.fault_banner.text()
    w.sim_controls["usb_failure"].setChecked(False)
    qtbot.mouseClick(w.clear_fault_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: w.connect_button.isEnabled(), timeout=WAIT)
    connect(qtbot, gui)


def test_disconnect(qtbot, gui):
    connect(qtbot, gui)
    qtbot.mouseClick(gui.window.disconnect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: state(gui) is State.DISCONNECTED, timeout=WAIT)
    assert gui.window.unit_label.text() == "Not connected"
    assert gui.window.connect_button.isEnabled()


def test_watchdog(qtbot, gui):
    connect(qtbot, gui)
    w = gui.window
    w._status_received_at -= 10          # as if no update arrived for 10 s
    w._check_watchdog()
    assert w.watchdog_banner.isVisible()
    assert not w.hv_on_button.isEnabled()
    assert w.estop_button.isEnabled()
    qtbot.waitUntil(lambda: not w.watchdog_banner.isVisible(), timeout=WAIT)


def test_watchdog_when_controller_stops(qtbot, gui):
    gui.controller.shutdown()
    gui.window._check_watchdog()
    assert "stopped" in gui.window.watchdog_banner.text()


def test_rating_required(qtbot):
    asked = []

    def ask(parent, serial):
        asked.append(serial)
        return 4.0, "label"

    g = make_gui(qtbot, fast_settings(units={}), ask_rating=ask)
    try:
        connect(qtbot, g)
        assert asked == [SERIAL]
        assert g.saved == [(SERIAL, 4.0, "label")]
        assert "4 W rating" in g.window.unit_label.text()
    finally:
        g.controller.shutdown()


def test_rating_declined(qtbot):
    g = make_gui(qtbot, fast_settings(units={}), ask_rating=lambda parent, serial: None)
    try:
        qtbot.mouseClick(g.window.connect_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: "no power rating" in log_text(g), timeout=WAIT)
        assert g.saved == [] and g.sims == []
        assert g.window.connect_button.isEnabled()
    finally:
        g.controller.shutdown()


def test_closing_with_hv_on_asks_and_switches_off(qtbot, gui):
    connect(qtbot, gui)
    switch_on(qtbot, gui)
    gui.confirmer.answer = False
    gui.window.close()
    assert gui.window.isVisible()                       # the operator said no
    gui.confirmer.answer = True
    gui.window.close()
    assert not gui.window.isVisible()
    assert not gui.controller.alive
    assert not gui.sims[-1].supply_on


# --- dialogs and widgets -----------------------------------------------------

def test_confirmer_revoke_closes_the_box(qtbot):
    confirmer = Confirmer()

    def answer_yes_after_revoke():
        box = QApplication.activeModalWidget()
        assert isinstance(box, QMessageBox)
        confirmer.revoke()

    QTimer.singleShot(100, answer_yes_after_revoke)
    assert confirmer.confirm(None, "Test", "Question?") is False
    assert not confirmer.open


def test_confirmer_yes(qtbot):
    confirmer = Confirmer()

    def press_yes():
        box = QApplication.activeModalWidget()
        box.done(QMessageBox.StandardButton.Yes)

    QTimer.singleShot(100, press_yes)
    assert confirmer.confirm(None, "Test", "Question?") is True


def test_rating_dialog(qtbot):
    dialog = RatingDialog(SERIAL)
    qtbot.addWidget(dialog)
    assert dialog.watts() == 4.0                        # the safe choice is the default
    assert not dialog._save.isEnabled()
    dialog.select(10.0, "  label  ")
    assert dialog._save.isEnabled()
    assert (dialog.watts(), dialog.source()) == (10.0, "label")


def test_lamp():
    lamp = Lamp()
    lamp.set("alarm", "Interlock OPEN")
    assert lamp.kind == "alarm" and "Interlock OPEN" in lamp.text()


# --- the application ---------------------------------------------------------

@pytest.fixture(autouse=True)
def restore_logging():
    import logging
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def test_app_runs_in_simulation(qtbot, tmp_path):
    config = tmp_path / "units.toml"
    config.write_text(f'[units."{SERIAL}"]\nwatt_max_w = 4.0\n\n'
                      f'[logging]\ndirectory = "{tmp_path / "logs"}"\n')
    seen = {}

    def drive():
        window = next(w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow))
        seen["title"] = window.windowTitle()
        qtbot.mouseClick(window.connect_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: "4 W rating" in window.unit_label.text(), timeout=WAIT)
        seen["title_connected"] = window.windowTitle()
        window.close()

    QTimer.singleShot(200, drive)
    assert app_module.main(["--sim", "--config", str(config)]) == 0
    assert seen["title"].endswith("[SIMULATION]")
    assert seen["title_connected"].endswith("[SIMULATION]")
    assert (tmp_path / "logs" / "minix.log").exists()
    assert list((tmp_path / "logs").glob("*_01300036.csv"))


def test_app_reports_a_bad_config(qtbot, tmp_path, monkeypatch):
    config = tmp_path / "units.toml"
    config.write_text('[units."01300036"]\nwatt_max_w = 7.0\n')
    shown = []
    monkeypatch.setattr(app_module.QMessageBox, "critical",
                        lambda parent, title, text: shown.append(text))
    assert app_module.main(["--config", str(config)]) == 2
    assert "4.0 or 10.0" in shown[0]
