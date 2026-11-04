"""The main window: connection, pages, banners, and the one flow that writes to a board."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, backup, config
from ..config import MC_ROOT
from ..errors import CommsError, SafetyRefusal, VerifyFailed
from ..link import Link, list_ports, open_serial
from . import theme
from .backups_page import BackupsPage
from .board import BoardThread
from .config_page import ConfigPage
from .console_page import ConsolePage
from .dashboard import Dashboard
from .prompts import Prompts
from .widgets import Banner
from .wording import humanize

SIMULATED_PORT = "Simulated board"


class MainWindow(QMainWindow):
    def __init__(self, *, link_factory=None, ports=list_ports, backup_dir=None, prompts=None, demo=None,
                 preset_port=None):  # fmt: skip
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(theme.app_icon())
        self.resize(1180, 780)
        self.demo = demo
        self.link_factory = link_factory or (lambda port: Link(open_serial(port)))
        if demo is not None:
            self.link_factory = lambda port: Link(demo)
            ports = lambda: [SIMULATED_PORT]  # noqa: E731
        self.ports, self.preset_port = ports, preset_port
        self.backup_dir = str(backup_dir or backup.default_dir())
        self.prompts = prompts or Prompts(self)
        self.board, self.info, self.layout = None, None, None
        self.script_paused = False
        self._connecting, self._closing = False, False

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        outer.addLayout(self._build_banners())
        body = QHBoxLayout()
        body.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFixedWidth(170)
        self.stack = QStackedWidget()
        self.dashboard, self.config_page = Dashboard(self), ConfigPage(self)
        self.backups_page, self.console = BackupsPage(self), ConsolePage(self)
        self.pages = (self.dashboard, self.config_page, self.backups_page, self.console)
        self.welcome = self._build_welcome()
        self.stack.addWidget(self.welcome)
        for name, page in zip(("Dashboard", "Configuration", "Backups", "Console"), self.pages, strict=True):
            self.nav.addItem(name)
            self.stack.addWidget(page)
        self.nav.currentRowChanged.connect(self._nav_changed)
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(140)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(QLabel("Stage 1. Not yet bench-tested on a real board."))

        self._ports_timer = QTimer(self)
        self._ports_timer.timeout.connect(self.refresh_ports)
        self._ports_timer.start(2000)
        self.refresh_ports()
        self._sync()
        if demo is not None:
            QTimer.singleShot(0, self.connect_board)

    # ---------------------------------------------------------------- construction
    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("Header")
        row = QHBoxLayout(header)
        row.setContentsMargins(14, 8, 14, 8)
        logo = QLabel()
        logo.setPixmap(theme.logo_pixmap(36, self.devicePixelRatioF()))
        logo.setAccessibleName("SkyScouter logo")
        title = QLabel(APP_NAME)
        title.setObjectName("AppTitle")
        self.board_label = QLabel("Not connected")
        self.board_label.setObjectName("Muted")
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(170)
        self.port_combo.lineEdit().setPlaceholderText("No board found")
        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("Primary")
        self.connect_button.clicked.connect(self.toggle_connection)
        row.addWidget(logo)
        row.addWidget(title)
        row.addSpacing(18)
        row.addWidget(self.board_label, 1)
        row.addWidget(self.port_combo)
        row.addWidget(self.connect_button)
        return header

    def _build_banners(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(0)
        self.demo_banner = Banner("demo")
        self.demo_banner.text.setText("<b>Simulated board.</b> Nothing here touches hardware, and its backups are "
                                      "deleted when you close the app.")  # fmt: skip
        self.turning_box = QCheckBox("Motor turning")
        self.turning_box.setToolTip("Simulate a turning engine, to see what the app refuses")
        self.demo_banner.row.addWidget(self.turning_box)
        if self.demo is not None:
            self.turning_box.toggled.connect(lambda on: setattr(self.demo, "turning", on))
            self.demo_banner.show()
        self.paused_banner = Banner("warning")
        self.paused_banner.text.setText("<b>⚠ Script paused by this app.</b> It has no throttle or kill control until "
                                        "you start it or power-cycle the board.")  # fmt: skip
        resume = QPushButton("Start script…")
        resume.clicked.connect(lambda: self.dashboard.start())
        self.paused_banner.row.addWidget(resume)
        self.baseline_banner = Banner("info")
        self.baseline_banner.text.setText("<b>No backup of this board yet.</b> Writes need one. Take it just after "
                                          "power-up with the engine stopped, before a script changes anything.")
        self.baseline_button = QPushButton("Back up now")
        self.baseline_button.clicked.connect(lambda: self.backups_page.backup_now())
        self.baseline_banner.row.addWidget(self.baseline_button)
        self.note_banner = Banner("warning")
        for b in (self.demo_banner, self.paused_banner, self.note_banner, self.baseline_banner):
            col.addWidget(b)
        return col

    def _build_welcome(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(theme.logo_pixmap(170, self.devicePixelRatioF()))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setAccessibleName("SkyScouter logo")
        title = QLabel(APP_NAME)
        title.setObjectName("PageTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel("Plug in the board and close VESC Tool, since only one program can use the port. Then click "
                      "Connect.\nConnecting only reads: nothing changes on the board until you confirm a write.")
        text.setObjectName("Muted")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setWordWrap(True)
        for w in (logo, title, text):
            lay.addWidget(w)
        return page

    # ---------------------------------------------------------------- connecting
    def refresh_ports(self) -> None:
        if self.board is not None:
            return
        found = self.ports()
        have = [self.port_combo.itemText(i) for i in range(self.port_combo.count())]
        if found == have:
            return
        typed = self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItems(found)
        if self.preset_port:
            self.port_combo.setCurrentText(self.preset_port)
        elif typed and typed in found:
            self.port_combo.setCurrentText(typed)
        elif len(found) == 1:
            self.port_combo.setCurrentIndex(0)
        else:  # with several boards, the operator picks: guessing could write to the wrong one
            self.port_combo.setCurrentIndex(-1)
            self.port_combo.clearEditText()
            self.port_combo.lineEdit().setPlaceholderText("Choose a port" if found else "No board found")

    def toggle_connection(self) -> None:
        if self.board is not None:
            self.disconnect_board()
        else:
            self.connect_board()

    def connect_board(self) -> None:
        port = self.port_combo.currentText().strip()
        if self.board is not None:
            return
        if not port:
            self.prompts.error("No port chosen", "Plug in the board, or choose or type its port.")
            return
        self.board = BoardThread(lambda: self.link_factory(port), self.backup_dir, self)
        self.board.opened.connect(self._on_opened)
        self.board.closed.connect(self._on_closed)
        self.board.sample.connect(self._on_sample)
        self.board.message.connect(self.log)
        self.board.busy_changed.connect(lambda _: self._sync())
        self.board.paused_changed.connect(self._on_paused)
        self._connecting = True
        self.board_label.setText(f"Connecting to {port}…")
        self.board.start()
        self._sync()

    def disconnect_board(self) -> None:
        if self.board is None or self.board.busy:
            return
        self.board.shutdown()
        self._sync()

    def _on_opened(self, info: dict) -> None:
        self._connecting = False
        self.info, self.layout = info, info["layout"]
        self.set_mode(info["mode"])
        self.baseline_banner.setVisible(info["layout"] is not None and not info["has_baseline"])
        self.note_banner.setVisible(bool(info["note"]))
        self.note_banner.text.setText(humanize(info["note"]))
        for page in self.pages:
            if hasattr(page, "on_connected"):
                page.on_connected(info)
        self.nav.setCurrentRow(0)
        self.stack.setCurrentWidget(self.dashboard)
        self._sync()

    def _on_closed(self, reason: str) -> None:
        board, was_paused = self.board, self.script_paused
        self.board, self.info, self.layout, self.script_paused = None, None, None, False
        self._connecting = False
        if board is not None:
            board.wait(2000)
            board.deleteLater()
        if self._closing:
            return
        for page in self.pages:
            page.reset()
        self.paused_banner.hide()
        self.baseline_banner.hide()
        self.note_banner.hide()
        self.board_label.setText("Not connected")
        self.stack.setCurrentWidget(self.welcome)
        self.nav.clearSelection()
        self._sync()
        if reason:
            self.log(f"Disconnected: {reason}")
            self.prompts.error("Disconnected", humanize(reason))
        if was_paused:
            self.prompts.info("The script is still paused", "The board keeps the script paused, with no throttle or "
                              "kill control, until it's started or the board is power-cycled.")  # fmt: skip

    def _on_sample(self, sample: dict) -> None:
        self.dashboard.on_sample(sample)
        self.console.on_prints(sample["prints"])

    def _on_paused(self, paused: bool) -> None:
        self.script_paused = paused
        self.paused_banner.setVisible(paused)

    # ---------------------------------------------------------------- shared by the pages
    def log(self, text: str) -> None:
        self.console.append(text)
        self.statusBar().showMessage(text.splitlines()[0] if text else "", 8000)

    def words(self, error) -> str:
        return humanize(str(error))

    def report(self, error) -> None:
        text = self.words(error)
        self.log(text)
        if isinstance(error, SafetyRefusal):
            self.prompts.error("Refused", text)
        elif isinstance(error, VerifyFailed):
            self.prompts.error("The write did not verify", text)
        elif isinstance(error, CommsError):
            self.prompts.error("Communication problem", text)
        else:
            self.prompts.error("Error", text)

    def set_mode(self, mode: str) -> None:
        if not self.info:
            return
        ident = self.info["ident"]
        parts = [ident["hw"], f"firmware {ident['fw']}"]
        if ident["test_version"]:
            parts.append(f"test build {ident['test_version']}")
        if mode:
            parts.append(mode.replace("MOTOR_TYPE_", ""))
        self.board_label.setText("  ·  ".join(parts))
        self.board_label.setToolTip(f"UUID {ident['uuid']}")

    def set_baseline_present(self) -> None:
        self.baseline_banner.hide()

    def write_flow(self, make_plan, after=None, nothing_text="Already set: nothing to write, so flash is left "
                   "alone.") -> None:  # fmt: skip
        """The only route to a board write: check, plan, show, typed confirmation, then Programmer.commit().

        commit() re-checks everything itself, so time spent in the dialog can't make the write unsafe.
        """
        board = self.board
        if board is None:
            return

        def plan_job(prog):
            prog.require_idle(prog.read_conf(MC_ROOT))
            return make_plan(prog)

        def planned(plan):
            if not plan.diff:
                self.prompts.info("Nothing to write", nothing_text)
                return
            lay = self.layout
            rows = [(k, config.show(lay, MC_ROOT, k, a), config.show(lay, MC_ROOT, k, b))
                    for k, (a, b) in sorted(plan.diff.items())]  # fmt: skip
            if not self.prompts.confirm_write(rows, plan.warnings):
                self.log("Cancelled. Nothing was written.")
                return
            if not board.submit(lambda prog: prog.commit(plan), wrote, failed, exclusive=True):
                self.prompts.error("Not written", "The board disconnected, so nothing was written.")

        def wrote(result):
            lay = self.layout
            lines = ["Written. The board's working copy reads back correct."]
            for k, (want, got) in result["limited"].items():
                lines.append(f"Limited by firmware: {k} asked {config.show(lay, MC_ROOT, k, want)}, "
                             f"stored {config.show(lay, MC_ROOT, k, got)}.")  # fmt: skip
            lines += ["The firmware doesn't report whether its flash save worked, so power-cycle and read again to "
                      "confirm a change you depend on.",
                      "The script is paused. Start it or power-cycle to run it again."]  # fmt: skip
            self.log(lines[0])
            self.set_baseline_present()
            self._after_write(after)
            self.prompts.info("Written", "\n\n".join(lines))

        def failed(error):
            self._after_write(after)
            self.report(error)

        board.submit(plan_job, planned, self.report, exclusive=True)

    def _after_write(self, after) -> None:
        self.backups_page.refresh()
        self.config_page.read() if after is None else after()

    # ---------------------------------------------------------------- state
    def _nav_changed(self, row: int) -> None:
        if row >= 0 and self.info:
            self.stack.setCurrentWidget(self.pages[row])

    def _sync(self) -> None:
        connected = self.info is not None and self.board is not None
        busy = self.board.busy if self.board else False
        config_ok = connected and self.layout is not None
        for page in self.pages:
            page.set_state(connected, busy, config_ok)
        self.nav.setEnabled(connected)
        self.port_combo.setEnabled(self.board is None)
        self.connect_button.setText("Disconnect" if self.board is not None else "Connect")
        self.connect_button.setEnabled(not self._connecting and not busy)
        self.baseline_button.setEnabled(config_ok and not busy)
        self.progress.setVisible(busy)

    def closeEvent(self, event) -> None:
        board = self.board
        if board is None:
            self._ports_timer.stop()
            event.accept()
            return
        if board.busy:
            self.prompts.info("Please wait", "The app is talking to the board. Closing now could interrupt a write.")
            event.ignore()
            return
        if self.script_paused and not self.prompts.confirm_close_paused():
            event.ignore()
            return
        self._closing = True
        self._ports_timer.stop()
        self.dashboard.stop_recording()
        board.shutdown()
        board.wait(10000)
        event.accept()
