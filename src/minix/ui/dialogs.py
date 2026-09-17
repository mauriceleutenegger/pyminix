"""Dialogs: power-rating entry and a revocable confirmation.

Confirmer asks yes/no questions. revoke() answers an open question with
"no", so the window can withdraw an HV-on prompt the moment the interlock
opens (§7.3); a revoked question returns False however it was answered.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QLabel, QLineEdit, QMessageBox,
    QRadioButton, QVBoxLayout, QWidget,
)

from ..config import VALID_RATINGS_W


class Confirmer:
    def __init__(self):
        self._box: QMessageBox | None = None
        self._revoked = False

    def confirm(self, parent: QWidget, title: str, text: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, title, text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        self._box, self._revoked = box, False
        try:
            answer = box.exec()
        finally:
            self._box = None
        return answer == QMessageBox.StandardButton.Yes and not self._revoked

    def revoke(self) -> None:
        if self._box is not None:
            self._revoked = True
            self._box.done(QMessageBox.StandardButton.No)

    @property
    def open(self) -> bool:
        return self._box is not None


class RatingDialog(QDialog):
    """Ask for a unit's power rating, when the controller does not state it."""

    def __init__(self, serial: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Power rating for Mini-X {serial}")
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"<p>Mini-X <b>{serial}</b> has no configured power rating. This controller does "
            "not state its rating (OEM controllers do); take it from the unit's label or "
            "documentation.</p>"
            "<p>Setting 10 W on a 4 W unit would allow the tube to be driven at 2.5 times its "
            "rating. <b>If you are unsure, choose 4 W</b>: under-stating the rating only "
            "limits the output.</p>")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._group = QButtonGroup(self)
        for watts in VALID_RATINGS_W:
            button = QRadioButton(f"{watts:g} W")
            button.setChecked(watts == min(VALID_RATINGS_W))
            self._group.addButton(button, int(watts))
            layout.addWidget(button)

        layout.addWidget(QLabel("Where does this rating come from?"))
        self._source = QLineEdit()
        self._source.setPlaceholderText("e.g. label on the unit, shipping documentation")
        layout.addWidget(self._source)

        self._confirmed = QCheckBox("I have checked this rating against the unit's documentation")
        layout.addWidget(self._confirmed)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        self._save = buttons.button(QDialogButtonBox.StandardButton.Save)
        self._save.setEnabled(False)
        self._confirmed.toggled.connect(self._save.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def watts(self) -> float:
        return float(self._group.checkedId())

    def source(self) -> str:
        return self._source.text().strip()

    def select(self, watts: float, source: str = "", confirmed: bool = True) -> None:
        self._group.button(int(watts)).setChecked(True)
        self._source.setText(source)
        self._confirmed.setChecked(confirmed)


def ask_rating(parent: QWidget, serial: str) -> tuple[float, str] | None:
    dialog = RatingDialog(serial, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.watts(), dialog.source()
