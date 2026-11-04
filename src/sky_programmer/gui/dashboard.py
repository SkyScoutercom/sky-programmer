"""Live values, charts, CSV recording and the script controls."""
from __future__ import annotations

import csv
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..programmer import MONITOR_COLUMNS
from .widgets import StripChart, Tile

STALE_AFTER_S = 1.5


class Dashboard(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._csv_file, self._csv = None, None
        self._last_sample = None
        self._last_fault = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        actions = QHBoxLayout()
        title = QLabel("Dashboard")
        title.setObjectName("PageTitle")
        self.record_button = QPushButton("Record CSV…")
        self.record_button.clicked.connect(self.toggle_recording)
        self.start_button = QPushButton("Start script…")
        self.start_button.clicked.connect(self.start)
        self.pause_button = QPushButton("Pause script…")
        self.pause_button.setObjectName("Danger")
        self.pause_button.setToolTip("Pauses the script and releases the motor. This does not stop the engine.")
        self.pause_button.clicked.connect(self.pause)
        actions.addWidget(title)
        actions.addStretch(1)
        for b in (self.record_button, self.start_button, self.pause_button):
            actions.addWidget(b)
        root.addLayout(actions)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.tiles = {}
        for i, (key, label) in enumerate((("bus", "Bus voltage"), ("erpm", "Speed"), ("motor_a", "Motor current"),
                                          ("input_a", "Input current"), ("duty", "Duty"), ("fet", "MOSFET temp"),
                                          ("motor", "Motor temp"), ("fault", "Fault"))):  # fmt: skip
            self.tiles[key] = Tile(label)
            grid.addWidget(self.tiles[key], i // 4, i % 4)
        root.addLayout(grid)

        charts = QGridLayout()
        charts.setSpacing(10)
        self.charts = {
            "erpm": StripChart("Speed", "erpm", 200, 0),
            "current_motor": StripChart("Motor current", "A", 4),
            "v_in": StripChart("Bus voltage", "V", 4),
            "temp_fet": StripChart("MOSFET temp", "°C", 10),
        }
        for i, chart in enumerate(self.charts.values()):
            charts.addWidget(chart, i // 2, i % 2)
        root.addLayout(charts, 1)

        note = QLabel("Stopping the engine is the RC switch's job: nothing on this page stops it.")
        note.setObjectName("Muted")
        root.addWidget(note)

        self._stale = QTimer(self)
        self._stale.timeout.connect(self._check_stale)
        self._stale.start(500)

    # ---------------------------------------------------------------- live data
    def on_sample(self, s: dict) -> None:
        self._last_sample = time.monotonic()
        v, t = s["values"], float(s["row"][0])
        self.tiles["bus"].set(f"{v['v_in']:.1f} V")
        self.tiles["erpm"].set(f"{v['erpm']:,} erpm")
        self.tiles["motor_a"].set(f"{v['current_motor']:.1f} A")
        self.tiles["input_a"].set(f"{v['current_in']:.1f} A")
        self.tiles["duty"].set(f"{v['duty'] * 100:.1f} %")
        self.tiles["fet"].set(f"{v['temp_fet']:.1f} °C")
        self.tiles["motor"].set(f"{v['temp_motor']:.1f} °C")
        name = s["fault"].replace("FAULT_CODE_", "")
        if v["fault_code"]:
            self.tiles["fault"].set(f"⚠ {name}", "critical")
        else:
            self.tiles["fault"].set(f"✓ {name}", "good")
        if v["fault_code"] and v["fault_code"] != self._last_fault:  # the tile clears, so keep a record
            self.win.log(f"Fault seen: {name}")
        self._last_fault = v["fault_code"]
        for key, chart in self.charts.items():
            chart.add(t, v[key])
        self._record(s["row"])

    def _check_stale(self) -> None:
        """Old numbers next to a motor are worse than none, e.g. during a write, when polling stops."""
        if self._last_sample and time.monotonic() - self._last_sample > STALE_AFTER_S:
            self._last_sample = None
            for tile in self.tiles.values():
                tile.set("—")
            for chart in self.charts.values():
                chart.mark_stale()

    def reset(self) -> None:
        self.stop_recording()
        self._last_sample = None
        self._last_fault = 0
        for tile in self.tiles.values():
            tile.set("—")
        for chart in self.charts.values():
            chart.clear()

    # ---------------------------------------------------------------- recording
    def toggle_recording(self) -> None:
        if self._csv:
            self.stop_recording()
            return
        path = self.win.prompts.ask_csv_path()
        if not path:
            return
        try:
            self._csv_file = open(path, "w", newline="")
            self._csv = csv.writer(self._csv_file)
            self._csv.writerow(MONITOR_COLUMNS)
        except OSError as e:
            self.stop_recording()
            self.win.prompts.error("Could not record", str(e))
            return
        self.record_button.setText("Stop recording")
        self.win.log(f"Recording to {path}")

    def _record(self, row: list) -> None:
        if not self._csv:
            return
        try:
            self._csv.writerow(row)
            self._csv_file.flush()
        except OSError as e:
            self.stop_recording()
            self.win.prompts.error("Recording stopped", f"The file can't be written any more: {e}")

    def stop_recording(self) -> None:
        handle, self._csv_file, self._csv = self._csv_file, None, None
        self.record_button.setText("Record CSV…")
        if handle:
            try:
                handle.close()
            except OSError:
                pass  # a disk that's full can't flush its last lines; the operator has been told

    # ---------------------------------------------------------------- script controls
    def pause(self) -> None:
        board = self.win.board
        if board is None:
            return

        def job(prog):
            prog.check_can_stop()  # again: the motor may have started during the prompt
            return prog.stop()

        def done(paused):
            self.win.log("Script paused, motor released." if paused else "No script on the board, motor released.")
            self.win.log("Note: C libraries' own threads keep running. This does not stop the engine.")

        def checked(_):
            if self.win.prompts.confirm_pause() and not board.submit(job, done, self.win.report, exclusive=True):
                self.win.prompts.error("Not paused", "The board disconnected, so the script was not paused.")

        board.submit(lambda prog: prog.check_can_stop(), checked, self.win.report, exclusive=True)

    def start(self) -> None:
        board = self.win.board
        if board is None:
            return

        def job(prog):
            prog.check_can_start()  # again: the motor may have started during the prompt
            return prog.start()

        def started(ok):
            self.win.log("Script started." if ok else "Could not start: is a script stored on the board?")
            if not ok:
                self.win.prompts.info("Script not started", "Could not start: is a script stored on the board?")

        def checked(_):
            if self.win.prompts.confirm_start() and not board.submit(job, started, self.win.report, exclusive=True):
                self.win.prompts.error("Not started", "The board disconnected, so the script was not started.")

        board.submit(lambda prog: prog.check_can_start(), checked, self.win.report, exclusive=True)

    def set_state(self, connected: bool, busy: bool, config_ok: bool) -> None:
        for b in (self.pause_button, self.start_button):
            b.setEnabled(connected and not busy)
        self.record_button.setEnabled(connected)
