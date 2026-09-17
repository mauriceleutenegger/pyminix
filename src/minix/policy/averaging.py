"""Running average of monitor readings (§6.4).

Reset it whenever the expected value changes (setpoint commit, HV on or
off, startup): the next reading then fills the whole window, so the
display does not crawl from stale values.
"""

from __future__ import annotations

from collections import deque

WINDOW = 7    # NumAVE in the reference


class RunningAverage:
    def __init__(self, size: int = WINDOW):
        if size < 1:
            raise ValueError("window size must be at least 1")
        self._size = size
        self._values: deque[float] = deque(maxlen=size)

    def reset(self) -> None:
        self._values.clear()

    def add(self, value: float) -> float:
        if not self._values:
            self._values.extend([value] * self._size)
        else:
            self._values.append(value)
        return self.value

    @property
    def value(self) -> float | None:
        if not self._values:
            return None
        return sum(self._values) / self._size
