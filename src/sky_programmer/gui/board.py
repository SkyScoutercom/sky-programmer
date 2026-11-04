"""The one thread that talks to the board.

Link isn't thread-safe and a request drains stale replies, so a poll racing a write could eat its reply. Everything that
touches the board therefore runs here, one job at a time, and polling only happens while no job is waiting. The GUI
never holds the Programmer: it submits a function, and gets the result back on its own thread.
"""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal

from .. import config
from ..config import MC_ROOT
from ..errors import CommsError, SafetyRefusal, SkyError
from ..programmer import Programmer

POLL_PERIOD_S = 0.1
POLL_FAILURES_ALLOWED = 2  # a single lost reply isn't a lost board


@dataclass
class _Job:
    token: int
    fn: Callable


class BoardThread(QThread):
    opened = Signal(dict)  # the board answered; see _open
    closed = Signal(str)  # the port is closed; the reason, or "" if the operator asked
    sample = Signal(dict)  # {"values", "fault", "row", "prints"}
    paused_changed = Signal(bool)  # whether this app has paused the script
    busy_changed = Signal(bool)
    message = Signal(str)
    _job_done = Signal(int, object, object)

    def __init__(self, open_link: Callable, backup_dir, parent=None, assume_paused=()):
        super().__init__(parent)
        self._open_link, self._backup_dir = open_link, backup_dir
        self._assume_paused = assume_paused  # boards this app paused earlier: reconnecting doesn't resume them
        self._jobs: queue.Queue = queue.Queue()
        self._callbacks: dict = {}
        self._next_token = 0
        self._exclusive_token = None
        self._shutting_down = False
        self._queue_lock = threading.Lock()  # so a job can't slip in after the worker has stopped for good
        self._paused = False
        self._job_done.connect(self._dispatch)

    # ------------------------------------------------------------------ used from the GUI thread
    @property
    def busy(self) -> bool:
        return self._exclusive_token is not None

    def submit(self, fn, on_ok=None, on_err=None, *, exclusive=False) -> bool:
        """Queue fn(programmer). False if it wasn't queued: closing, or an exclusive job is already pending."""
        with self._queue_lock:
            if self._shutting_down or (exclusive and self.busy):
                return False
            self._next_token += 1
            token = self._next_token
            self._callbacks[token] = (on_ok, on_err)
            if exclusive:
                self._exclusive_token = token
            self._jobs.put(_Job(token, fn))
        if exclusive:
            self.busy_changed.emit(True)
        return True

    def shutdown(self) -> None:
        """Close after the jobs already queued. A write in progress is always finished first."""
        with self._queue_lock:
            self._shutting_down = True
            self._jobs.put(None)

    def _dispatch(self, token, result, error) -> None:
        on_ok, on_err = self._callbacks.pop(token, (None, None))
        if token == self._exclusive_token:  # before the callback, which may submit the next step
            self._exclusive_token = None
            self.busy_changed.emit(False)
        if error is not None:
            if on_err:
                on_err(error)
        elif on_ok:
            on_ok(result)

    # ------------------------------------------------------------------ the worker thread
    def run(self) -> None:
        reason, link = "", None
        try:
            link = self._open_link()
            self._prog = Programmer(link, self._backup_dir, say=self.message.emit)
            self.opened.emit(self._open())
            self._report_paused()
            reason = self._loop()
        except SkyError as e:
            reason = str(e)
        except Exception as e:  # e.g. a device that isn't a VESC sends a reply that can't be parsed
            reason = f"unexpected reply from the board: {e!r}"
        finally:
            self._cancel_queued()
            try:
                if link is not None:
                    link.close()
            except Exception as e:  # a vanished device can fail to close; the window must still hear about it
                self.message.emit(f"Closing the port failed: {e}")
            self.closed.emit(reason)

    def _open(self) -> dict:
        prog = self._prog
        ident, v = prog.identify(), prog.values()
        prog.script_paused = ident["uuid"] in self._assume_paused
        info = {"ident": ident, "values": v, "fault": prog.fault_name(v["fault_code"]), "layout": None, "note": "",
                "mode": "", "has_baseline": False}  # fmt: skip
        try:
            lay = prog.layout()
            info["layout"] = lay
            info["has_baseline"] = prog.baseline() is not None
            info["mode"] = config.show(lay, MC_ROOT, "motor_type", prog.read_conf(MC_ROOT)["motor_type"])
        except SafetyRefusal as e:  # unsupported firmware, or a script changing the config right now
            info["note"] = str(e)
        return info

    def _loop(self) -> str:
        failures, t0 = 0, time.monotonic()
        while True:
            try:
                job = self._jobs.get(timeout=POLL_PERIOD_S)
            except queue.Empty:
                try:
                    self._poll(t0)
                    failures = 0
                except SkyError as e:
                    failures += 1
                    if failures >= POLL_FAILURES_ALLOWED:
                        return f"lost contact with the board: {e}"
                continue
            if job is None:
                return ""
            self._run_job(job)

    def _poll(self, t0: float) -> None:
        """Reading values doesn't disturb a paused script. The firmware's Lisp stats command would resume it."""
        prog = self._prog
        v = prog.values()
        prog.link.drain()
        row = prog.monitor_row(v, time.monotonic() - t0)
        self.sample.emit({"values": v, "fault": prog.fault_name(v["fault_code"]), "row": row,
                          "prints": prog.link.take_prints()})  # fmt: skip
        self._report_paused()

    def _run_job(self, job: _Job) -> None:
        result, error = None, None
        try:
            result = job.fn(self._prog)
        except SkyError as e:
            error = e
        except Exception as e:
            error = SkyError(f"unexpected problem: {e!r}")
        self._report_paused()
        self._job_done.emit(job.token, result, error)

    def _report_paused(self) -> None:
        if self._prog.script_paused != self._paused:
            self._paused = self._prog.script_paused
            self.paused_changed.emit(self._paused)

    def _cancel_queued(self) -> None:
        """Whatever was still waiting gets an answer, so nothing waits forever on a board that's gone."""
        with self._queue_lock:
            self._shutting_down = True
            while True:
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    return
                if job is not None:
                    self._job_done.emit(job.token, None, CommsError("the board disconnected before this ran."))
