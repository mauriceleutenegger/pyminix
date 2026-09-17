"""Small display widgets: status lamps, the power bar, the X-ray indicator, banners."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QProgressBar, QSizePolicy

from ..policy.banding import PowerBand

GREY = "#9e9e9e"
GREEN = "#2e7d32"
AMBER = "#ef6c00"
RED = "#c62828"

BAND_COLOURS = {
    PowerBand.IDLE: "#e0e0e0",
    PowerBand.NORMAL: "#43a047",
    PowerBand.CAUTION: "#fbc02d",
    PowerBand.DANGER: "#e53935",
}


class Lamp(QLabel):
    """A coloured dot with a label."""

    COLOURS = {"off": GREY, "ok": GREEN, "warn": AMBER, "alarm": RED}

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.kind = "off"
        self.set("off", text)

    def set(self, kind: str, text: str) -> None:
        self.kind = kind
        colour = self.COLOURS[kind]
        weight = "bold" if kind in ("warn", "alarm") else "normal"
        self.setText(f'<span style="color:{colour}; font-size:16pt">●</span> '
                     f'<span style="font-weight:{weight}">{text}</span>')


class PowerBar(QProgressBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextVisible(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.band: PowerBand | None = None
        self.clear()

    def clear(self) -> None:
        self.band = None
        self.setRange(0, 1)
        self.setValue(0)
        self.setFormat("—")
        self._colour(BAND_COLOURS[PowerBand.IDLE])

    def set_power(self, mw: float, band: PowerBand, rated_mw: float) -> None:
        self.band = band
        self.setRange(0, int(rated_mw))
        self.setValue(int(min(max(mw, 0.0), rated_mw)))
        self.setFormat(f"{mw:.0f} mW  ({band.value})")
        self._colour(BAND_COLOURS[band])

    def _colour(self, colour: str) -> None:
        self.setStyleSheet(f"QProgressBar::chunk {{ background-color: {colour}; }}"
                           "QProgressBar { color: black; }")


class XrayIndicator(QLabel):
    """A large label that blinks while HV may be on."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(44)
        self.on = False
        self._lit = False
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._blink)
        self._show()

    def set_on(self, on: bool) -> None:
        if on == self.on:
            return
        self.on = on
        self._lit = on
        if on:
            self._timer.start()
        else:
            self._timer.stop()
        self._show()

    def _blink(self) -> None:
        self._lit = not self._lit
        self._show()

    def _show(self) -> None:
        if self.on:
            background = RED if self._lit else "#ffcdd2"
            self.setText("X-RAYS ON")
            self.setStyleSheet(f"background:{background}; color:white; font-size:18pt;"
                               "font-weight:bold; border-radius:6px;")
        else:
            self.setText("X-rays off")
            self.setStyleSheet(f"background:#eeeeee; color:{GREY}; font-size:18pt;"
                               "border-radius:6px;")


class Banner(QLabel):
    STYLES = {
        "error": f"background:{RED}; color:white;",
        "warning": f"background:{AMBER}; color:white;",
        "info": "background:#e3f2fd; color:#0d47a1;",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.level: str | None = None
        self.hide()

    def show_message(self, level: str, text: str) -> None:
        self.level = level
        self.setText(text)
        self.setStyleSheet(self.STYLES[level] + "padding:6px; font-weight:bold;")
        self.show()

    def clear(self) -> None:
        self.level = None
        self.hide()
