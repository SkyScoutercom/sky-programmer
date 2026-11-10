"""This board's Lisp script: back up, try from RAM, store and restore.

Everything goes through code.py, the same engine as `sky code`, so the checks the bench test passed apply here too. Each
board action is checked first, then asked about, then run as one job, which checks again itself.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import code
from ..config import MC_ROOT
from ..errors import SafetyRefusal

NOTE = ("A stored script runs at every power-up. Try a script first: it runs from RAM until the next power-up and "
        "stores nothing. Storing backs up the old script and the config, then reads every byte back. Scripts with "
        "native libraries can be stored but not tried, and get a start-up guard: they wait 10 s after each power-up "
        "before loading a library, so `sky code rescue` in a terminal can break a boot loop.")  # fmt: skip


def _precheck(prog) -> None:
    """Before asking the operator anything: the job itself checks all of this again."""
    code.require_vesc_hardware(prog)
    prog.require_idle(prog.read_conf(MC_ROOT))


class ScriptsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.paths = []
        self._last_dir = str(Path.home())
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)
        bar = QHBoxLayout()
        title = QLabel("Scripts")
        title.setObjectName("PageTitle")
        bar.addWidget(title)
        bar.addStretch(1)
        self.try_button = QPushButton("Try a file…")
        self.try_button.clicked.connect(self.try_file)
        self.store_button = QPushButton("Store a file…")
        self.store_button.clicked.connect(self.store_file)
        self.restore_button = QPushButton("Restore…")
        self.restore_button.clicked.connect(self.restore)
        self.backup_button = QPushButton("Back up stored script")
        self.backup_button.setObjectName("Primary")
        self.backup_button.clicked.connect(self.backup_now)
        for b in (self.try_button, self.store_button, self.restore_button, self.backup_button):
            bar.addWidget(b)
        root.addLayout(bar)
        note = QLabel(NOTE)
        note.setObjectName("Muted")
        note.setWordWrap(True)
        root.addWidget(note)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("Saved", "Reason", "Size", "Imported files"))
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        root.addWidget(self.table, 1)
        self._enabled, self._busy = False, False
        self._sync_buttons()

    # ---------------------------------------------------------------- the list of script backups
    def refresh(self) -> None:
        info = self.win.info
        if not info:
            return
        self.paths = list(reversed(code.list_backups(self.win.backup_dir, info["ident"]["uuid"])))
        self.table.setRowCount(len(self.paths))
        for row, path in enumerate(self.paths):
            try:
                doc = code.load_backup(path)
                size = "No script" if doc["empty"] else f"{len(doc['content'])} bytes"
                cells = (doc["created"][:19].replace("T", "  "), doc["reason"], size, str(doc["imports"]))
            except SafetyRefusal:
                cells = (path.name, "Damaged file, can't be used", "", "")
            for col, text in enumerate(cells):
                self.table.setItem(row, col, QTableWidgetItem(text))
        self.table.clearSelection()
        self._sync_buttons()

    def _selected_doc(self):
        row = self.table.currentRow()
        if not self.table.selectedItems() or row < 0:
            return None
        try:
            return code.load_backup(self.paths[row])
        except SafetyRefusal as e:
            self.win.report(e)
        return None

    def _refresh_lists(self) -> None:
        """Every board action may have saved backups. Refreshing must never swallow the message about the action."""
        try:
            self.refresh()
            self.win.backups_page.refresh()
        except Exception as e:
            self.win.log(f"Could not refresh the backup lists: {e!r}")

    def _failed(self, error) -> None:
        self._refresh_lists()
        self.win.report(error)

    # ---------------------------------------------------------------- board actions
    def backup_now(self) -> None:
        board = self.win.board
        if board is None:
            return

        def job(prog):
            content = code.read_code(prog)
            return code.save_backup(prog, content, "manual"), content

        def done(result):
            path, content = result
            if content:
                imports = code.import_count(content)
                text = f"Script backup saved ({len(content)} bytes, {imports} imported files): {path}"
            else:
                text = f"No valid script is stored on the board. Saved an empty backup: {path}"
            self.win.log(text)
            self._refresh_lists()
            self.win.prompts.info("Script backed up", text)

        board.submit(job, done, self._failed, exclusive=True)

    def _pick_script(self, *, native: bool):
        """(path, text, warnings) of a script that passed the content checks, or None. native: storing allows them."""
        chosen = self.win.prompts.ask_script_path(self._last_dir)
        if not chosen:
            return None
        path = Path(chosen)
        self._last_dir = str(path.parent)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            self.win.prompts.error("Can't read the script", f"{path}: {e}")
            return None
        try:
            return path, text, code.check_script(text, native=native)
        except SafetyRefusal as e:
            self.win.report(e)
            return None

    def try_file(self) -> None:
        board, picked = self.win.board, self._pick_script(native=False)
        if board is None or picked is None:
            return
        path, text, warnings = picked

        def done(res):
            self.win.log(f"Running {path.name} from RAM ({res['bytes']} bytes). The stored script is unchanged.")
            self.win.log(f"Config backed up: {res['config_backup']}")
            self._refresh_lists()
            self.win.prompts.info("Script running", f"{path.name} is running from RAM. The stored script is unchanged. "
                                  "Its messages, and any error in it, show in the Console. Power-cycle the board, or "
                                  "click Start script on the Dashboard, to go back to the stored script.")  # fmt: skip

        def checked(_):
            if not self.win.prompts.confirm_try(path.name, len(text.encode("utf-8")), warnings):
                self.win.log("Cancelled. Nothing was sent.")
                return
            if not board.submit(lambda prog: code.try_script(prog, text), done, self._failed, exclusive=True):
                self.win.prompts.error("Not run", "The board disconnected, so the script wasn't sent.")

        board.submit(_precheck, checked, self.win.report, exclusive=True)

    def store_file(self) -> None:
        picked = self._pick_script(native=True)
        if picked is None:
            return
        path, text, warnings = picked
        try:
            content, files = code.pack_script(text, path.parent)
        except SafetyRefusal as e:
            self.win.report(e)
            return
        if code.uses_native(text) and not self.win.prompts.confirm_native(
                len(files), f"{code.NATIVE_RISK} {code.GUARD_NOTE}"):  # fmt: skip
            self.win.log("Cancelled. Nothing was written.")
            return
        self._store(content, path.name, warnings)

    def restore(self) -> None:
        doc = self._selected_doc()
        if doc is None or not self.win.info:
            return
        try:
            imports = code.check_restore(doc, self.win.info["ident"])
        except SafetyRefusal as e:
            self.win.report(e)
            return
        risk = code.NATIVE_RISK
        if not code.has_guard(code.script_text(doc["content"])):
            risk += (" This backup has no start-up guard, so `sky code rescue` can't help if its library crashes. "
                     "Storing the script from its file adds one.")  # fmt: skip
        if imports and not self.win.prompts.confirm_native(imports, risk):
            self.win.log("Cancelled. Nothing was written.")
            return
        self._store(doc["content"], f"the script backup from {doc['created'][:19].replace('T', ' ')}", [])

    def _store(self, content: bytes, what: str, warnings) -> None:
        board = self.win.board
        if board is None:
            return

        def done(res):
            lines = [f"Stored and read back: all {res['bytes']} bytes match.",
                     f"Previous script backed up: {res['code_backup']}",
                     f"Config backed up: {res['config_backup']}",
                     "The new script is stored but not running. Start it next, or later from the Dashboard, or "
                     "power-cycle the board."]  # fmt: skip
            for line in lines:
                self.win.log(line)
            self._refresh_lists()
            self.win.prompts.info("Script stored", "\n\n".join(lines))
            self.win.dashboard.start()  # its own check and typed YES; cancelling leaves it stopped

        def checked(_):
            if not self.win.prompts.confirm_store(what, len(content), warnings, self.win.write_target()):
                self.win.log("Cancelled. Nothing was written.")
                return
            if not board.submit(lambda prog: code.store(prog, content), done, self._failed, exclusive=True):
                self.win.prompts.error("Not written", "The board disconnected, so nothing was written.")

        board.submit(_precheck, checked, self.win.report, exclusive=True)

    # ---------------------------------------------------------------- state
    def _sync_buttons(self) -> None:
        idle = self._enabled and not self._busy
        self.backup_button.setEnabled(idle)
        self.try_button.setEnabled(idle)
        self.store_button.setEnabled(idle)
        self.restore_button.setEnabled(idle and bool(self.table.selectedItems()))

    def on_connected(self, info: dict) -> None:
        if info["layout"] is not None:
            self.refresh()

    def reset(self) -> None:
        self.paths = []
        self.table.setRowCount(0)
        self._sync_buttons()

    def set_state(self, connected: bool, busy: bool, config_ok: bool) -> None:
        self._enabled, self._busy = connected and config_ok, busy  # script upload is only reviewed for firmware 7.00
        self._sync_buttons()
