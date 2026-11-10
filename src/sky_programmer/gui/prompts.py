"""Every question the app asks the operator. Tests replace this class, so no test depends on clicking a dialog."""
from __future__ import annotations

from html import escape

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
STARTER_NOTE = ("If the script can run a starter or an engine, set its input to off first, for example the RC channel "
                "that selects OFF. On the bench, an engine script cranked the starter at IDLE.")  # fmt: skip
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

    def __init__(self, parent, title, body, prompt, action, rows=None, warnings=(), word="YES", headers=None,
                 danger=False):  # fmt: skip
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
        self.go.setObjectName("Danger" if danger else "Primary")
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
        where = f"<b>{escape(target)}</b>" if target else "the board"
        body = f"This is what would change on {where}."
        return self._typed("Review changes", body, ENGINE_PROMPT, "Write to board", rows, warnings)

    def confirm_start(self) -> bool:
        return self._typed("Start script", "", "This runs the script stored on the board, which may move the motor or "
                           "servo. Type YES:", "Start script")  # fmt: skip

    def confirm_pause(self) -> bool:
        return self._typed("Pause script", PAUSE_TEXT, PAUSE_PROMPT, "Pause script")

    def confirm_try(self, name: str, size: int, warnings) -> bool:
        body = (f"This runs <b>{escape(name)}</b> ({size} bytes) from RAM in place of the stored script, until the "
                "board is power-cycled. Nothing is stored. Its messages, and any error in it, show in the Console."
                f"<br><br>{STARTER_NOTE}")  # fmt: skip
        return self._typed("Try script", body, "It may move the motor or servo. Engine stopped, propeller clear? Type "
                           "YES to run it:", "Run script", warnings=warnings)  # fmt: skip

    def confirm_store(self, what: str, size: int, warnings, target: str) -> bool:
        body = (f"This stores <b>{escape(what)}</b> ({size} bytes) on <b>{escape(target)}</b>, which runs it at every "
                "power-up from now on. The old script and the config are backed up first, and every byte is read back "
                "after writing. The running script stops, and every erase and write releases the motor for up to "
                f"5 s.<br><br>{STARTER_NOTE}")  # fmt: skip
        return self._typed("Store script", body, ENGINE_PROMPT, "Store on board", warnings=warnings)

    def confirm_native(self, imports: int, native_risk: str, *, storing: bool = False, libraries=()) -> bool:
        what = "This script uses native libraries" if storing else "This backup includes imported files"
        body = f"{what} ({imports} imported).<br><br>{escape(native_risk)}"
        if libraries:
            body += "<br><br>" + "<br>".join(escape(line) for line in libraries)
        title, prompt = (("Store a native library?", "Type YES to store it:") if storing else
                         ("Restore a native library?", "Type YES to restore it anyway:"))  # fmt: skip
        return self._typed(title, body, prompt, "Continue", danger=True)

    def ask_script_path(self, start: str) -> str:
        return QFileDialog.getOpenFileName(self.parent, "Choose a Lisp script", start,
                                           "LispBM (*.lisp *.lbm);;All files (*)")[0]  # fmt: skip

    def confirm_new_baseline(self, rows) -> bool:
        return self._typed("Back up these changes too?", BASELINE_TEXT, "Type YES to make these the board's settings:",
                           "Back up", rows, headers=("Field", "Last backup", "Board now"))  # fmt: skip

    def confirm_reset(self, preview: dict) -> bool:
        lines = [f"Reset <b>{escape(preview['dir'])}</b> to <b>{escape(preview['target'])}</b>. "
                 "Uncommitted changes can't be brought back."]  # fmt: skip
        if preview["note"]:
            lines.append(escape(preview["note"]))
        rows = [("Merge in progress", "It will be abandoned")] if preview["merge"] else []
        rows += [("Uncommitted change", line) for line in preview["lost_files"]]
        rows += [("Local commit", line) for line in preview["dropped"]]
        rows += [("Untracked file, replaced", line) for line in preview["overwritten"]]
        if not rows:
            lines.append("Nothing would be lost: this folder has no uncommitted changes or local commits.")
        if preview["untracked"]:
            lines.append(f"{preview['untracked']} other untracked file(s) will be kept.")
        return self._typed("Reset the source folder", "<br><br>".join(lines), "Type RESET to discard what's listed:",
                           "Reset", rows, word="RESET", headers=("What's lost", "Item"), danger=True)  # fmt: skip

    def _yes(self, title, text) -> bool:
        box = QMessageBox.warning(self.parent, title, text, QMessageBox.StandardButton.Yes
                                  | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)  # fmt: skip
        return box == QMessageBox.StandardButton.Yes

    def confirm_repl_resume(self) -> bool:
        return self._typed("This resumes the script", "The script may be paused, and any REPL expression resumes it, "
                           "which may move the motor or servo.", "Type YES to send it anyway:", "Send")  # fmt: skip

    def confirm_power_cycled(self) -> bool:
        return self._yes("Was it power-cycled?", "Only say yes if the board really was power-cycled since the pause. "
                         "If it wasn't, the script is still paused, with no throttle or kill control, and this "
                         "warning goes away.")  # fmt: skip

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
