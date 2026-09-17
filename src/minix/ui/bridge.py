"""Qt adapter for the controller's Listener.

The controller calls the listener on its worker thread. QtBridge lives in
the GUI thread, so Qt queues these signals to the GUI thread's slots.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..controller import Event, Status


class QtBridge(QObject):
    status_received = Signal(object)    # Status
    event_received = Signal(object)     # Event

    def status(self, status: Status) -> None:
        self.status_received.emit(status)

    def event(self, event: Event) -> None:
        self.event_received.emit(event)
