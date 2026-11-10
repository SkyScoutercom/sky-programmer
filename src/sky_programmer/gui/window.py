"""The main window: connection, pages, banners, and the one flow that writes to a board."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, backup, config, gitops
from ..config import MC_ROOT
from ..errors import CommsError, SafetyRefusal, VerifyFailed
from ..link import Link, list_ports, open_serial
from ..paths import config_dir
from . import theme
from .backups_page import BackupsPage
from .board import BoardThread
from .config_page import ConfigPage
from .console_page import ConsolePage
from .dashboard import Dashboard
from .demo import DemoBoard
from .prompts import Prompts
from .rescue import RescueThread
from .scripts_page import ScriptsPage
from .updates_page import UpdatesPage
from .widgets import Banner, Pill
from .wording import humanize

SIMULATED_PORT = "Simulated board"
NAV = ("Dashboard", "Configuration", "Backups", "Scripts", "Console", "Updates")


def _default_settings_path() -> Path:
    return config_dir() / "settings.ini"


class MainWindow(QMainWindow):
    def __init__(self, *, link_factory=None, ports=list_ports, backup_dir=None, prompts=None, demo=None,
                 preset_port=None, settings_path=None, repo_dir=None):  # fmt: skip
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(theme.app_icon())
        self.resize(1240, 820)
        self.settings = QSettings(str(settings_path or _default_settings_path()), QSettings.Format.IniFormat)
        self.theme_name = str(self.settings.value("theme", theme.system_theme()))
        theme.apply(self.theme_name)

        self.link_factory = link_factory or (lambda port: Link(open_serial(port)))
        self.ports = ports  # real boards only: the simulated one is always added after them
        self._port_chosen = bool(preset_port)
        self._real_backup_dir = str(backup_dir or backup.default_dir())
        self.backup_dir = self._real_backup_dir  # switches to a temporary folder while the simulated board is in use
        self.demo = None  # the simulated board, while it's the one connected
        self._next_demo = demo  # started with --simulate: connect to this one first
        self._sim_dir = None
        self.prompts = prompts or Prompts(self)
        self.board, self.info, self.layout = None, None, None
        self.script_paused = False
        self.code_stale = False
        # This app's pauses of real boards, remembered across a reconnect or restart. The board can't be asked.
        saved = str(self.settings.value("paused_boards", ""))
        self._paused_uuids: set = {uuid for uuid in saved.split(",") if uuid}
        self._connecting, self._closing = False, False
        self._rescue, self._rescue_dialog = None, None  # a rescue running with no board connected

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        body = QHBoxLayout()
        body.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFixedWidth(186)
        self.nav.setIconSize(QSize(20, 20))
        self.stack = QStackedWidget()
        self.stack.setObjectName("Pages")
        self.dashboard, self.config_page = Dashboard(self), ConfigPage(self)
        self.backups_page, self.console = BackupsPage(self), ConsolePage(self)
        self.scripts_page = ScriptsPage(self)
        saved_repo = self.settings.value("repo")
        # A packaged .exe has no source folder, and a pull couldn't change it: a new version is a new download.
        self.packaged = bool(getattr(sys, "frozen", False))
        checkout = None if self.packaged else gitops.find_checkout()
        self.updates_page = UpdatesPage(self, repo_dir or saved_repo or checkout)
        self.board_pages = (self.dashboard, self.config_page, self.backups_page, self.scripts_page, self.console)
        self.pages = (*self.board_pages, self.updates_page)
        self.welcome = self._build_welcome()
        self.stack.addWidget(self.welcome)
        for name, page in zip(NAV, self.pages, strict=True):
            self.nav.addItem(QListWidgetItem(name))
            self.stack.addWidget(page)
        self._paint_nav_icons()
        self.nav.item(NAV.index("Updates")).setHidden(self.packaged)
        self.nav.currentRowChanged.connect(self._nav_changed)
        body.addWidget(self.nav)
        right = QVBoxLayout()
        right.setSpacing(0)
        right.addLayout(self._build_banners())
        right.addWidget(self.stack, 1)
        body.addLayout(right, 1)
        outer.addLayout(body, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(140)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(QLabel("Bench-tested on one board: see docs/BENCH_RESULTS.md"))

        self._ports_timer = QTimer(self)
        self._ports_timer.timeout.connect(self.refresh_ports)
        self._ports_timer.start(2000)
        if preset_port:
            self.port_combo.setCurrentText(preset_port)
        self.refresh_ports()
        self._sync()
        if demo is not None:
            QTimer.singleShot(0, self.connect_simulated)

    # ---------------------------------------------------------------- construction
    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("Header")
        row = QHBoxLayout(header)
        row.setContentsMargins(14, 9, 14, 9)
        row.setSpacing(10)
        logo = QLabel()
        logo.setObjectName("LogoChip")  # the logo's dark blue needs a light background at night
        logo.setPixmap(theme.logo_pixmap(30, self.devicePixelRatioF()))
        logo.setAccessibleName("SkyScouter logo")
        title = QLabel(APP_NAME)
        title.setObjectName("AppTitle")
        self.pill = Pill()
        self.board_label = QLabel("")
        self.board_label.setObjectName("Muted")
        self.theme_button = QPushButton()
        self.theme_button.setObjectName("ThemeToggle")
        self.theme_button.clicked.connect(self.toggle_theme)
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(190)
        self.port_combo.setAccessibleName("Serial port")
        self.port_combo.lineEdit().setPlaceholderText("No board found")
        self.port_combo.lineEdit().textEdited.connect(self._port_touched)
        self.port_combo.activated.connect(self._port_touched)
        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("Primary")
        self.connect_button.setMinimumWidth(104)
        self.connect_button.clicked.connect(self.toggle_connection)
        row.addWidget(logo)
        row.addWidget(title)
        row.addSpacing(8)
        row.addWidget(self.pill)
        row.addWidget(self.board_label, 1)
        row.addWidget(self.theme_button)
        row.addWidget(self.port_combo)
        row.addWidget(self.connect_button)
        self._paint_theme_button()
        return header

    def _build_banners(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(0)
        self.demo_banner = Banner("demo")
        self.demo_banner.text.setText("<b>Simulated board.</b> Nothing here touches hardware, and its backups are "
                                      "deleted when you close the app.")  # fmt: skip
        self.turning_box = QCheckBox("Motor turning")
        self.turning_box.setToolTip("Simulate a turning engine, to see what the app refuses")
        self.turning_box.toggled.connect(lambda on: self.demo is not None and setattr(self.demo, "turning", on))
        self.demo_banner.row.addWidget(self.turning_box)
        self.stale_banner = Banner("warning")
        self.stale_banner.text.setText("<b>⚠ The code in this app's folder changed.</b> Restart before connecting, so "
                                       "the checks that run are the ones in the folder.")  # fmt: skip
        restart = QPushButton("Restart now")
        restart.clicked.connect(self.restart_app)
        self.stale_banner.row.addWidget(restart)
        self.paused_banner = Banner("warning")
        self.paused_banner.text.setText("<b>⚠ The script may be paused by this app.</b> A paused script has no "
                                        "throttle or kill control until it's started or the board is power-cycled.")
        resume = QPushButton("Start script…")
        resume.clicked.connect(lambda: self.dashboard.start())
        forget = QPushButton("I power-cycled it…")
        forget.setToolTip("Power-cycling restarts the script, and the board can't tell this app that it happened")
        forget.clicked.connect(self.forget_pause)
        self.paused_banner_buttons = (forget, resume)
        self.paused_banner.row.addWidget(forget)
        self.paused_banner.row.addWidget(resume)
        self.baseline_banner = Banner("info")
        self.baseline_banner.text.setText("<b>No backup of this board yet.</b> Writes need one. Take it just after "
                                          "power-up with the engine stopped, before a script changes anything.")
        self.baseline_button = QPushButton("Back up now")
        self.baseline_button.clicked.connect(lambda: self.backups_page.backup_now())
        self.baseline_banner.row.addWidget(self.baseline_button)
        self.note_banner = Banner("warning")
        for b in (self.demo_banner, self.stale_banner, self.paused_banner, self.note_banner, self.baseline_banner):
            col.addWidget(b)
        return col

    def _build_welcome(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card = QFrame()
        card.setObjectName("Card")
        card.setMaximumWidth(520)
        col = QVBoxLayout(card)
        col.setContentsMargins(36, 30, 36, 30)
        col.setSpacing(14)
        logo = QLabel()
        logo.setObjectName("LogoChipLarge")
        logo.setPixmap(theme.logo_pixmap(130, self.devicePixelRatioF()))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setAccessibleName("SkyScouter logo")
        title = QLabel(APP_NAME)
        title.setObjectName("PageTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel("<b>1.</b> Plug in the board.<br><b>2.</b> Close VESC Tool: only one program can use the "
                      "port.<br><b>3.</b> Click <b>Connect</b>.<br><br>Connecting only reads. Nothing changes on the "
                      "board until you confirm a write.")  # fmt: skip
        text.setWordWrap(True)
        self.try_simulated_button = QPushButton("Try with a simulated board")
        self.try_simulated_button.setToolTip("Everything works as with a real board, and nothing touches hardware")
        self.try_simulated_button.clicked.connect(self.connect_simulated)
        self.rescue_button = QPushButton("Rescue a boot-looping board…")
        self.rescue_button.setToolTip("Erase the stored script as the board powers up, when a native library keeps "
                                      "it rebooting")  # fmt: skip
        self.rescue_button.clicked.connect(self.start_rescue)
        for w in (logo, title, text, self.try_simulated_button, self.rescue_button):
            col.addWidget(w)
        lay.addWidget(card, 0, Qt.AlignmentFlag.AlignCenter)
        return page

    # ---------------------------------------------------------------- day and night
    def toggle_theme(self) -> None:
        self.set_theme("night" if self.theme_name == "day" else "day")

    def set_theme(self, name: str) -> None:
        self.theme_name = theme.apply(name).name
        self.settings.setValue("theme", self.theme_name)
        self._paint_theme_button()
        self._paint_nav_icons()
        self.config_page.retheme()
        self.update()

    def _paint_theme_button(self) -> None:
        night = self.theme_name == "night"
        self.theme_button.setText("☾  Night" if night else "☀  Day")
        self.theme_button.setToolTip("Switch to day" if night else "Switch to night")
        self.theme_button.setAccessibleName(f"Theme: {'night' if night else 'day'}. Click to switch.")

    def _paint_nav_icons(self) -> None:
        for row, name in enumerate(NAV[: self.nav.count()]):
            self.nav.item(row).setIcon(theme.nav_icon(name))

    # ---------------------------------------------------------------- connecting
    def _port_touched(self, *_) -> None:
        self._port_chosen = bool(self.port_combo.currentText().strip())

    def refresh_ports(self) -> None:
        """A port the operator chose or typed is never replaced. Otherwise fill in the only board, and never guess.

        The simulated board is always offered last, and never chosen automatically.
        """
        if self.board is not None:
            return
        found = self.ports()
        items = [*found, SIMULATED_PORT]
        have = [self.port_combo.itemText(i) for i in range(self.port_combo.count())]
        if items == have:
            return
        text = self.port_combo.currentText()
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        self.port_combo.addItems(items)
        if self._port_chosen:
            self.port_combo.setCurrentText(text)
        elif len(found) == 1:
            self.port_combo.setCurrentIndex(0)
        else:
            self.port_combo.setCurrentIndex(-1)
            self.port_combo.clearEditText()
            self.port_combo.lineEdit().setPlaceholderText("Choose a port" if found else "No board found")
        self.port_combo.blockSignals(False)

    def toggle_connection(self) -> None:
        if self.board is not None:
            self.disconnect_board()
        else:
            self.connect_board()

    def connect_board(self) -> None:
        port = self.port_combo.currentText().strip()
        if self.board is not None:
            return
        if self.code_stale:
            self.prompts.info("Restart first", "The code in this app's folder changed. Restart before connecting.")
            return
        if self.updates_page.busy:
            self.prompts.info("Please wait", "An update is running. Connect when it has finished.")
            return
        if self._rescue is not None:
            self.prompts.info("Please wait", "A rescue is running. Connect when it has finished.")
            return
        if not port:
            self.prompts.error("No port chosen", "Plug in the board, or choose or type its port.")
            return
        self.port_in_use = port
        if port == SIMULATED_PORT:
            # Its own temporary folder, so a simulated backup can never be taken for a real board's, and no remembered
            # pause of a real board applies to it.
            demo, self._next_demo = self._next_demo or DemoBoard(), None
            if self._sim_dir is None:
                self._sim_dir = tempfile.TemporaryDirectory(prefix="sky-simulated-", ignore_cleanup_errors=True)
            self.demo, self.backup_dir = demo, self._sim_dir.name
            open_link, assume_paused = (lambda: Link(demo)), set()
        else:
            self.demo, self.backup_dir = None, self._real_backup_dir
            open_link, assume_paused = (lambda: self.link_factory(port)), set(self._paused_uuids)
        self.demo_banner.setVisible(self.demo is not None)
        self.board = BoardThread(open_link, self.backup_dir, self, assume_paused)
        self.board.opened.connect(self._on_opened)
        self.board.closed.connect(self._on_closed)
        self.board.sample.connect(self._on_sample)
        self.board.message.connect(self.log)
        self.board.busy_changed.connect(lambda _: self._sync())
        self.board.paused_changed.connect(self._on_paused)
        self._connecting = True
        self.pill.show_state("busy", "◌  Connecting")
        self.board_label.setText(port)
        self.board.start()
        self._sync()

    def connect_simulated(self) -> None:
        if self.board is not None:
            return
        self.port_combo.setCurrentText(SIMULATED_PORT)
        self._port_chosen = True
        self.connect_board()

    def disconnect_board(self) -> None:
        if self.board is None or self.board.busy:
            return
        self.board.shutdown()
        self._sync()

    def _on_opened(self, info: dict) -> None:
        self._connecting = False
        self.info, self.layout = info, info["layout"]
        self.pill.show_state("ok", "●  Simulated board" if self.demo is not None else "●  Connected")
        self.set_mode(info["mode"])
        self.baseline_banner.setVisible(info["layout"] is not None and not info["has_baseline"])
        self.note_banner.setVisible(bool(info["note"]))
        self.note_banner.text.setText(humanize(info["note"]))
        for page in self.board_pages:
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
        for page in self.board_pages:
            page.reset()
        self.demo, self.backup_dir = None, self._real_backup_dir
        self.turning_box.setChecked(False)
        self.demo_banner.hide()
        self.paused_banner.hide()
        self.baseline_banner.hide()
        self.note_banner.hide()
        self.pill.show_state("idle", "○  Not connected")
        self.board_label.setText("")
        if self.stack.currentWidget() is not self.updates_page:
            self.stack.setCurrentWidget(self.welcome)
            self.nav.setCurrentRow(-1)
        self._sync()
        if reason:
            self.log(f"Disconnected: {reason}")
            self.prompts.error("Disconnected", humanize(reason))
        if was_paused:
            self.prompts.info("The script may still be paused", "A paused script stays paused, with no throttle or "
                              "kill control, until it's started or the board is power-cycled. If the board restarted, "
                              "the script is running again.")  # fmt: skip

    def _on_sample(self, sample: dict) -> None:
        self.dashboard.on_sample(sample)
        self.console.on_prints(sample["prints"])

    def _on_paused(self, paused: bool) -> None:
        self.script_paused = paused
        self.paused_banner.setVisible(paused)
        if self.info and self.demo is None:  # a simulated board's pause is never remembered as a real one's
            uuid = self.info["ident"]["uuid"]
            (self._paused_uuids.add if paused else self._paused_uuids.discard)(uuid)
            self.settings.setValue("paused_boards", ",".join(sorted(self._paused_uuids)))

    def forget_pause(self) -> None:
        if self.board is not None and self.prompts.confirm_power_cycled():
            self.board.submit(lambda prog: prog.forget_pause(), exclusive=True)

    # ---------------------------------------------------------------- updates
    def remember_repo(self, path) -> None:
        self.settings.setValue("repo", str(path))

    def code_changed(self, running_from_repo: bool, repo) -> None:
        if running_from_repo:
            self.code_stale = True
            self.stale_banner.show()
            self._sync()
        else:
            self.prompts.info("Folder updated", "This app is an installed copy, so it still runs the old code. To use "
                              f"the new code, run:\n\npipx install --force --editable '{repo}[gui]'")  # fmt: skip

    def restart_app(self) -> None:
        if self.close():
            QProcess.startDetached(sys.executable, ["-m", "sky_programmer.gui", *sys.argv[1:]])

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
        parts.append(f"UUID …{ident['uuid'][-6:]}")
        self.board_label.setText("  ·  ".join(parts))
        self.board_label.setToolTip(f"UUID {ident['uuid']}")

    def set_baseline_present(self) -> None:
        self.baseline_banner.hide()

    def write_target(self) -> str:
        ident = self.info["ident"]
        return f"{ident['hw']} on {self.port_in_use}, UUID {ident['uuid']}"

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
            rows = [(k, *config.show_pair(lay, MC_ROOT, k, a, b)) for k, (a, b) in sorted(plan.diff.items())]
            if not self.prompts.confirm_write(rows, plan.warnings, self.write_target()):
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
            lines.append("The firmware doesn't report whether its flash save worked, so power-cycle and read again to "
                         "confirm a change you depend on.")  # fmt: skip
            if self.script_paused:
                lines.append("The script is paused. Start it or power-cycle to run it again.")
            self.log(lines[0])
            self.set_baseline_present()
            self._after_write(after)
            self.prompts.info("Written", "\n\n".join(lines))

        def failed(error):
            self._after_write(after)
            self.report(error)

        board.submit(plan_job, planned, self.report, exclusive=True)

    def _after_write(self, after) -> None:
        """Refreshing a list must never swallow the message about the write itself."""
        try:
            self.backups_page.refresh()
            self.config_page.read() if after is None else after()
        except Exception as e:
            self.log(f"Could not refresh the page after the write: {e!r}")

    # ---------------------------------------------------------------- state
    def _nav_changed(self, row: int) -> None:
        if row < 0:
            return
        page = self.pages[row]
        if page is self.updates_page or self.info:
            self.stack.setCurrentWidget(page)
            if page is self.updates_page:
                self.updates_page.refresh()

    def sync_ui(self) -> None:
        self._sync()

    def _sync(self) -> None:
        connected = self.info is not None and self.board is not None
        busy = self.board.busy if self.board else False
        config_ok = connected and self.layout is not None
        for page in self.pages:
            page.set_state(connected, busy, config_ok)
        for row in range(len(self.board_pages)):
            item = self.nav.item(row)
            flags = item.flags()
            enabled = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            item.setFlags(flags | enabled if connected else flags & ~enabled)
        self.port_combo.setEnabled(self.board is None)
        self.connect_button.setText("Disconnect" if self.board is not None else "Connect")
        self.connect_button.setEnabled(not self._connecting and not busy and not self.updates_page.busy
                                       and self._rescue is None)  # fmt: skip
        self.rescue_button.setEnabled(self.board is None and self._rescue is None and not self._connecting)
        self.baseline_button.setEnabled(config_ok and not busy)
        for button in self.paused_banner_buttons:
            button.setEnabled(connected and not busy)
        self.progress.setVisible(busy)

    # ---------------------------------------------------------------- rescue
    def start_rescue(self) -> None:
        """Erase the stored script as the board powers up. Runs with no board connected: it makes its own."""
        if self.board is not None or self._rescue is not None:
            self.prompts.info("Disconnect first", "The rescue needs the board's port to itself. Disconnect, then "
                              "start it again.")  # fmt: skip
            return
        if not self.prompts.confirm_rescue():
            self.log("Cancelled. Nothing was erased.")
            return
        thread = RescueThread(self.link_factory, self.ports, self._real_backup_dir, self)
        self._rescue = thread
        thread.message.connect(self.log)
        thread.done.connect(self._rescue_done)
        self._rescue_dialog = self.prompts.rescue_progress(thread)
        thread.start()
        self._sync()

    def _rescue_done(self) -> None:
        thread, dialog = self._rescue, self._rescue_dialog
        self._rescue, self._rescue_dialog = None, None
        thread.wait()
        if dialog is not None:
            dialog.accept()
        self._sync()
        if isinstance(thread.error, SafetyRefusal) and "cancelled" in str(thread.error):
            self.log(self.words(thread.error))
        elif thread.error is not None:
            self.report(thread.error)
        else:
            res = thread.result
            text = (f"Erased. {res['hw']}, UUID {res['uuid']}, now starts with no script. Its script was backed up "
                    "first: it's on the Scripts page once you connect. Fix the library before storing it again.")
            self.log(text)
            self.prompts.info("Rescued", text)
        thread.deleteLater()

    def closeEvent(self, event) -> None:
        if self._rescue is not None:
            self.prompts.info("Please wait", "A rescue is running. Cancel it first, then close.")
            event.ignore()
            return
        if self.updates_page.busy:
            self.prompts.info("Please wait", "An update is running. Closing now could leave the folder half changed.")
            event.ignore()
            return
        if self.board is None:
            self._ports_timer.stop()
            self._remove_simulated_backups()
            event.accept()
            return
        if self.board.busy:
            self.prompts.info("Please wait", "The app is talking to the board. Closing now could interrupt a write.")
            event.ignore()
            return
        if self.script_paused and not self.prompts.confirm_close_paused():
            event.ignore()
            return
        self._closing = True
        self._ports_timer.stop()
        self.dashboard.stop_recording()
        board = self.board  # read again: the board may have gone during the question above
        if board is not None:
            board.shutdown()
            if not board.wait(10000):  # stuck in the USB driver. No write is running: closing was refused above
                board.terminate()
                board.wait(2000)
        self._remove_simulated_backups()
        event.accept()

    def _remove_simulated_backups(self) -> None:
        if self._sim_dir is not None:
            self._sim_dir.cleanup()
            self._sim_dir = None
