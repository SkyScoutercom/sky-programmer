"""The simulated board with live-looking values, for trying the app with no hardware (`sky-gui --simulate`)."""
from __future__ import annotations

import math
import time

from .. import protocol as p
from ..simulator import FakeVesc


class DemoBoard(FakeVesc):
    def __init__(self):
        super().__init__()
        self.turning = False
        self._t0 = time.monotonic()
        self._next_print = self._t0 + 5

    def read(self, n):
        now = time.monotonic()
        if self.script_running and now >= self._next_print:  # a paused script goes quiet, as on a real board
            self._next_print = now + 5
            self._print(p.COMM_LISP_PRINT, "starter: waiting for RC channel 8")
        if not self.tx:
            time.sleep(0.01)  # real serial reads wait; this keeps the simulation from using a whole CPU core
            return b""
        return super().read(n)

    def _values(self):
        t = time.monotonic() - self._t0
        self.v_in = 48.0 + 0.3 * math.sin(t / 4)
        self.temp_fet = 26.0 + 1.5 * math.sin(t / 15)
        self.temp_motor = 24.0 + math.sin(t / 22)
        if self.turning:
            self.erpm = int(3200 + 600 * math.sin(t / 3))
            self.current, self.duty = 14.0 + 4 * math.sin(t / 3), 0.32
        else:
            self.erpm, self.current, self.duty = 0, 0.0, 0.0
        return super()._values()
