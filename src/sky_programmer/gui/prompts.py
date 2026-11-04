"""Every question the app asks the operator. Tests replace this class, so no test depends on clicking a dialog."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..cli import ENGINE_PROMPT

PAUSE_TEXT = ("Pausing takes away the script's throttle and kill control, and it does not stop the engine. An engine "
              "running behind the relay is invisible to the board, so this check can't see it.")  # fmt: skip
PAUSE_PROMPT = "Engine stopped? Type YES to pause the script and release the motor:"
BASELINE_TEXT = ("The board differs from its last backup. If a script made these changes, they're temporary, and a new "
                 "backup would make them this board's settings, so the next write would store them. Power-cycle first "
                 "to clear them, or back up anyway if you changed these on purpose.")  # fmt: skip


def rows_table(headers, rows) -> QTableWidget:
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().hide()
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            table.setItem(r, c, QTableWidgetItem(str(text)))
    return table


class TypedConfirm(QDialog):
    """A click is too easy for a statement about an engine the board can't see, so the word is typed, as in `sky`.

    Enter only works once the word is right, and Esc cancels.
    """

    def __init__(self, parent, title, body, prompt, action, rows=None, warnings=(), word="YES", headers=None):
        super().__init__(parent)
        self.word = word
        self.setWindowTitle(title)
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        if body:
            text = QLabel(body)
            text.setWordWrap(True)
            lay.addWidget(text)
        if rows:
            lay.addWidget(rows_table(headers or ("Field", "Now", "After"), rows))
        for w in warnings:
            note = QLabel(f"⚠ {w}")
            note.setWordWrap(True)
            lay.addWidget(note)
        label = QLabel(prompt)
        label.setWordWrap(True)
        lay.addWidget(label)
        self.answer = QLineEdit()
        self.answer.setPlaceholderText(word)
        lay.addWidget(self.answer)
        self.buttons = QDialogButtonBox()
        self.go = self.buttons.addButton(action, QDialogButtonBox.ButtonRole.AcceptRole)
        self.go.setObjectName("Primary")
        self.go.setEnabled(False)
        self.go.setAutoDefault(False)
        cancel = self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setAutoDefault(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.answer.textChanged.connect(lambda t: self.go.setEnabled(t.strip() == self.word))
        self.answer.returnPressed.connect(self._enter)
        self.answer.setFocus()

    def _enter(self) -> None:
        if self.go.isEnabled():
            self.accept()


class Prompts:
    def __init__(self, parent):
        self.parent = parent

    def _typed(self, *args, **kwargs) -> bool:
        return TypedConfirm(self.parent, *args, **kwargs).exec() == QDialog.DialogCode.Accepted

    def confirm_write(self, rows, warnings, target="") -> bool:
        body = f"This is what would change on <b>{target}</b>." if target else "This is what would change on the board."
        return self._typed("Review changes", body, ENGINE_PROMPT, "Write to board", rows, warnings)

    def confirm_start(self) -> bool:
        return self._typed("Start script", "", "This runs the script stored on the board, which may move the motor or "
                           "servo. Type YES:", "Start script")  # fmt: skip

    def confirm_pause(self) -> bool:
        return self._typed("Pause script", PAUSE_TEXT, PAUSE_PROMPT, "Pause script")

    def confirm_new_baseline(self, rows) -> bool:
        return self._typed("Back up these changes too?", BASELINE_TEXT, "Type YES to make these the board's settings:",
                           "Back up", rows, headers=("Field", "Last backup", "Board now"))  # fmt: skip

    def confirm_reset(self, preview: dict) -> bool:
        lines = [f"Reset this folder to <b>{preview['target']}</b>. It can't be undone from this app."]
        if preview["note"]:
            lines.append(preview["note"])
        rows = [("Uncommitted change", line) for line in preview["lost_files"]]
        rows += [("Local commit", line) for line in preview["dropped"]]
        if not rows:
            lines.append("Nothing would be lost: this folder has no uncommitted changes or local commits.")
        if preview["untracked"]:
            lines.append(f"{preview['untracked']} untracked file(s) will be kept.")
        return self._typed("Reset the source folder", "<br><br>".join(lines), "Type RESET to discard what's listed:",
                           "Reset", rows, word="RESET", headers=("What's lost", "Item"))  # fmt: skip

    def _yes(self, title, text) -> bool:
        box = QMessageBox.warning(self.parent, title, text, QMessageBox.StandardButton.Yes
                                  | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)  # fmt: skip
        return box == QMessageBox.StandardButton.Yes

    def confirm_repl_resume(self) -> bool:
        return self._yes("This resumes the script", "The script may be paused, and any REPL expression resumes it, "
                         "which may move the motor or servo. Send it anyway?")  # fmt: skip

    def confirm_close_paused(self) -> bool:
        return self._yes("The script may be paused", "A paused script stays paused after you close this app, with no "
                         "throttle or kill control, until it's started or the board is power-cycled. "
                         "Close anyway?")  # fmt: skip

    def error(self, title, text) -> None:
        QMessageBox.critical(self.parent, title, text)

    def info(self, title, text) -> None:
        QMessageBox.information(self.parent, title, text)

    def show_differences(self, title, rows) -> None:
        dialog = QDialog(self.parent)
        dialog.setWindowTitle(title)
        dialog.setMinimumSize(640, 380)
        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.addWidget(rows_table(("Field", "Backup", "Board"), rows))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        lay.addWidget(buttons)
        dialog.exec()

    def ask_csv_path(self) -> str:
        return QFileDialog.getSaveFileName(self.parent, "Record live values", "sky-log.csv", "CSV (*.csv)")[0]

    def ask_folder(self, start: str) -> str:
        return QFileDialog.getExistingDirectory(self.parent, "Choose the sky-programmer folder", start)
