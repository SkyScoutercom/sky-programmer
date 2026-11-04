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
    """A click is too easy for a statement about an engine the board can't see, so YES is typed, as in the terminal."""

    def __init__(self, parent, title, body, prompt, action, rows=None, warnings=()):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        if body:
            text = QLabel(body)
            text.setWordWrap(True)
            lay.addWidget(text)
        if rows:
            lay.addWidget(rows_table(("Field", "Now", "After"), rows))
        for w in warnings:
            note = QLabel(f"⚠ {w}")
            note.setWordWrap(True)
            lay.addWidget(note)
        lay.addWidget(QLabel(prompt))
        self.answer = QLineEdit()
        self.answer.setPlaceholderText("YES")
        lay.addWidget(self.answer)
        self.buttons = QDialogButtonBox()
        self.go = self.buttons.addButton(action, QDialogButtonBox.ButtonRole.AcceptRole)
        self.go.setObjectName("Primary")
        self.go.setEnabled(False)
        self.go.setAutoDefault(False)
        cancel = self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.answer.textChanged.connect(lambda t: self.go.setEnabled(t.strip() == "YES"))
        self.answer.setFocus()


class Prompts:
    def __init__(self, parent):
        self.parent = parent

    def confirm_write(self, rows, warnings) -> bool:
        dialog = TypedConfirm(self.parent, "Review changes", "This is what would change on the board.",
                              ENGINE_PROMPT, "Write to board", rows, warnings)  # fmt: skip
        return dialog.exec() == QDialog.DialogCode.Accepted

    def confirm_start(self) -> bool:
        dialog = TypedConfirm(self.parent, "Start script", "", "This runs the script stored on the board, which may "
                              "move the motor or servo. Type YES:", "Start script")  # fmt: skip
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _yes(self, title, text) -> bool:
        box = QMessageBox.warning(self.parent, title, text, QMessageBox.StandardButton.Yes
                                  | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)  # fmt: skip
        return box == QMessageBox.StandardButton.Yes

    def confirm_repl_resume(self) -> bool:
        return self._yes("This resumes the script", "This app paused the script, and any REPL expression resumes it, "
                         "which may move the motor or servo. Send it anyway?")  # fmt: skip

    def confirm_close_paused(self) -> bool:
        return self._yes("The script is paused", "The script stays paused after you close this app, with no throttle "
                         "or kill control, until it's started or the board is power-cycled. Close anyway?")  # fmt: skip

    def error(self, title, text) -> None:
        QMessageBox.critical(self.parent, title, text)

    def info(self, title, text) -> None:
        QMessageBox.information(self.parent, title, text)

    def show_differences(self, title, rows) -> None:
        dialog = QDialog(self.parent)
        dialog.setWindowTitle(title)
        dialog.setMinimumSize(620, 360)
        lay = QVBoxLayout(dialog)
        lay.addWidget(rows_table(("Field", "Backup", "Board"), rows))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        lay.addWidget(buttons)
        dialog.exec()

    def ask_csv_path(self) -> str:
        return QFileDialog.getSaveFileName(self.parent, "Record live values", "sky-log.csv", "CSV (*.csv)")[0]
