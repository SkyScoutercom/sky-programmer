"""This board's saved configs: back up, compare and restore."""
from __future__ import annotations

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

from .. import backup, config
from ..config import MC_ROOT
from ..errors import SafetyRefusal


class BackupsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.paths = []
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)
        bar = QHBoxLayout()
        title = QLabel("Backups")
        title.setObjectName("PageTitle")
        bar.addWidget(title)
        bar.addStretch(1)
        self.compare_button = QPushButton("Compare with board")
        self.compare_button.clicked.connect(self.compare)
        self.restore_button = QPushButton("Restore…")
        self.restore_button.clicked.connect(self.restore)
        self.backup_button = QPushButton("Back up now")
        self.backup_button.setObjectName("Primary")
        self.backup_button.clicked.connect(self.backup_now)
        for b in (self.compare_button, self.restore_button, self.backup_button):
            bar.addWidget(b)
        root.addLayout(bar)
        note = QLabel("Every write saves a backup first. A restore changes the motor config only, and keeps this "
                      "board's own calibration. App config is saved, but only VESC Tool can write it back.")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        root.addWidget(note)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("Saved", "Reason", "Board", "Firmware"))
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

    def refresh(self) -> None:
        info = self.win.info
        if not info:
            return
        self.paths = list(reversed(backup.list_backups(self.win.backup_dir, info["ident"]["uuid"])))
        self.table.setRowCount(len(self.paths))
        for row, path in enumerate(self.paths):
            try:
                doc = backup.load(path)
                cells = (doc["created"][:19].replace("T", "  "), doc["reason"], doc["hw"], doc["fw"])
            except (OSError, ValueError, KeyError, SafetyRefusal):
                cells = (path.name, "Damaged file, can't be used", "", "")
            for col, text in enumerate(cells):
                self.table.setItem(row, col, QTableWidgetItem(str(text)))
        self.table.clearSelection()
        self._sync_buttons()

    def _selected_doc(self):
        row = self.table.currentRow()
        if not self.table.selectedItems() or row < 0:
            return None
        try:
            return backup.load(self.paths[row])
        except SafetyRefusal as e:
            self.win.report(e)
        except (OSError, ValueError, KeyError) as e:
            self.win.prompts.error("Can't read this backup", str(e))
        return None

    # ---------------------------------------------------------------- board actions
    def backup_now(self) -> None:
        board = self.win.board
        if board is None:
            return

        def done(path):
            self.win.log(f"Backup saved: {path}")
            self.win.set_baseline_present()
            self.refresh()

        def checked(drift):
            """A new backup becomes the baseline, so it must not quietly accept a script's temporary changes."""
            if drift:
                lay = self.win.layout
                rows = [(k, *config.show_pair(lay, MC_ROOT, k, a, b)) for k, (a, b) in sorted(drift.items())]
                if not self.win.prompts.confirm_new_baseline(rows):
                    self.win.log("Cancelled. No backup was saved.")
                    return
            if not board.submit(lambda prog: prog.save_backup(), done, self.win.report, exclusive=True):
                self.win.prompts.error("Not saved", "The board disconnected, so no backup was saved.")

        board.submit(lambda prog: prog.baseline_drift(), checked, self.win.report, exclusive=True)

    def compare(self) -> None:
        doc = self._selected_doc()
        if doc is None:
            return

        def done(diff):
            lay = self.win.layout
            if not diff:
                self.win.prompts.info("No differences", "The board matches this backup.")
                return
            rows = [(k, config.show(lay, MC_ROOT, k, a), config.show(lay, MC_ROOT, k, b))
                    for k, (a, b) in sorted(diff.items())]  # fmt: skip
            self.win.prompts.show_differences(f"{len(rows)} differences", rows)

        self.win.board.submit(lambda prog: prog.compare_backup(doc), done, self.win.report)

    def restore(self) -> None:
        doc = self._selected_doc()
        if doc is not None:
            self.win.write_flow(lambda prog: prog.plan_restore(doc), nothing_text="The board already matches this "
                                "backup, so nothing was written.")  # fmt: skip

    # ---------------------------------------------------------------- state
    def _sync_buttons(self) -> None:
        idle = self._enabled and not self._busy
        picked = bool(self.table.selectedItems())
        self.backup_button.setEnabled(idle)
        self.compare_button.setEnabled(idle and picked)
        self.restore_button.setEnabled(idle and picked)

    def on_connected(self, info: dict) -> None:
        if info["layout"] is not None:
            self.refresh()

    def reset(self) -> None:
        self.paths = []
        self.table.setRowCount(0)
        self._sync_buttons()

    def set_state(self, connected: bool, busy: bool, config_ok: bool) -> None:
        self._enabled, self._busy = connected and config_ok, busy
        self._sync_buttons()
