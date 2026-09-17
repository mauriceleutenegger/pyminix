"""Application entry point.

    minix-gui                 # real hardware
    minix-gui --sim           # the simulated controller, with a fault panel
    minix-gui --config PATH   # a specific units.toml
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from ..config import (
    ConfigError, Settings, UnitEntry, default_paths, load_settings, save_unit,
)
from ..controller import Controller, FanOut
from ..discovery import ControllerInfo, list_controllers
from ..sim import SimHarness
from ..telemetry import RunRecorder, setup_logging, software_version
from ..transport import FtdiTransport
from .bridge import QtBridge
from .main_window import MainWindow

log = logging.getLogger(__name__)

SIM_SERIAL = "01300036"


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="minix-gui", description="Mini-X X-ray tube controller")
    ap.add_argument("--sim", action="store_true", help="use the simulated controller")
    ap.add_argument("--config", type=Path, help="units.toml to use")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    app = QApplication.instance() or QApplication([sys.argv[0], *argv])
    app.setApplicationName("minix")

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        QMessageBox.critical(None, "Configuration error", str(exc))
        return 2
    try:
        log_path = setup_logging(settings.logging.directory)
    except OSError as exc:
        QMessageBox.critical(None, "Logging error",
                             f"cannot write to {settings.logging.directory}: {exc}")
        return 2
    log.info("%s starting%s; settings from %s; log %s", software_version(),
             " in simulation mode" if args.sim else "", settings.path or "defaults", log_path)

    harness = SimHarness() if args.sim else None
    if harness is not None:
        factory = harness.create

        def discover() -> list[ControllerInfo]:
            return [ControllerInfo(SIM_SERIAL, "MiniX Controller (simulated)", None, None)]

        def save_rating(serial: str, watts: float, source: str) -> Settings:
            # Simulation never writes the real configuration file.
            nonlocal settings
            units = {**settings.units, serial: UnitEntry(watts, source or "simulation")}
            settings = dataclasses.replace(settings, units=units)
            settings.unit(serial)
            return settings
    else:
        factory = FtdiTransport.open
        discover = list_controllers

        def save_rating(serial: str, watts: float, source: str) -> Settings:
            path = settings.path or default_paths()[0]
            save_unit(path, serial, watts, source)
            log.info("saved a %g W rating for %s to %s", watts, serial, path)
            return load_settings(path)

    bridge = QtBridge()
    recorder = RunRecorder(settings.logging.directory, sample_hz=settings.logging.sample_hz)
    controller = Controller(settings, transport_factory=factory,
                            listener=FanOut(bridge, recorder))
    controller.start()

    def recording() -> str:
        if recorder.error:
            return recorder.error
        paths = recorder.paths
        return f"Recording to {paths[0]}" if paths else "Not recording"

    window = MainWindow(controller, bridge, settings, discover=discover,
                        save_rating=save_rating, recording=recording,
                        sim=harness,
                        title_suffix=" [SIMULATION]" if args.sim else "")
    window.resize(900, 820)
    window.show()
    try:
        code = app.exec()
    finally:
        if not controller.shutdown():
            log.error("the controller did not stop in time; check the tube")
        recorder.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
