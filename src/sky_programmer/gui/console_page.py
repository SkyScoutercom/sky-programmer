"""Board messages, read-only terminal commands and the filtered REPL. Motor words are never allowed here."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import safety
from ..errors import SafetyRefusal


class ConsolePage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)
        title = QLabel("Console")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        note = QLabel("Script and C library messages appear here. Any REPL expression resumes a paused script. Words "
                      "that write flash, reach other boards or move the motor are blocked, but functions your own "
                      "script defines aren't checked.")  # fmt: skip
        note.setObjectName("Muted")
        note.setWordWrap(True)
        root.addWidget(note)
        self.output = QPlainTextEdit()
        self.output.setObjectName("Output")
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(5000)
        root.addWidget(self.output, 1)

        term = QHBoxLayout()
        term.addWidget(QLabel("Terminal"))
        self.term_choice = QComboBox()
        self.term_choice.addItems(sorted(safety.TERMINAL_READ_ONLY))
        self.term_button = QPushButton("Run")
        self.term_button.clicked.connect(self.run_terminal)
        term.addWidget(self.term_choice, 1)
        term.addWidget(self.term_button)
        root.addLayout(term)

        repl = QHBoxLayout()
        repl.addWidget(QLabel("REPL"))
        self.repl_input = QLineEdit()
        self.repl_input.setPlaceholderText("(+ 1 2)")
        self.repl_input.setMaxLength(safety.REPL_MAX_LEN)
        self.repl_input.returnPressed.connect(self.send_repl)
        self.repl_button = QPushButton("Send")
        self.repl_button.clicked.connect(self.send_repl)
        repl.addWidget(self.repl_input, 1)
        repl.addWidget(self.repl_button)
        root.addLayout(repl)

    def append(self, text: str) -> None:
        self.output.appendPlainText(text)

    def on_prints(self, lines: list) -> None:
        for line in lines:
            self.append(f"[board] {line}")

    def _show_reply(self, lines) -> None:
        for line in lines or ["(no reply)"]:
            self.append(line)

    def _show_error(self, error) -> None:
        prefix = "Refused" if isinstance(error, SafetyRefusal) else "Error"
        self.append(f"{prefix}: {self.win.words(error)}")

    def run_terminal(self) -> None:
        board = self.win.board
        if board is None:
            return
        command = self.term_choice.currentText()
        if board.submit(lambda prog: prog.terminal(command), self._show_reply, self._show_error, exclusive=True):
            self.append(f"term> {command}")

    def send_repl(self) -> None:
        text = self.repl_input.text()
        board = self.win.board
        if not text.strip() or board is None or board.busy:
            return
        if self.win.script_paused and not text.strip().startswith(":") and not self.win.prompts.confirm_repl_resume():
            self.append("Not sent: the script stays paused.")
            return
        resuming = self.win.script_paused and not text.strip().startswith(":")

        def job(prog):
            if resuming:  # the same check as Start script, since this can restart it
                prog.check_can_start()
            return prog.repl(text)

        if board.submit(job, self._show_reply, self._show_error, exclusive=True):
            self.append(f"sky> {text}")
            self.repl_input.clear()
        else:
            self.append("Not sent: the board has disconnected.")

    def reset(self) -> None:
        pass  # the log stays readable after a disconnect

    def on_connected(self, info: dict) -> None:
        ident = info["ident"]
        self.append(f"Connected: {ident['hw']}, firmware {ident['fw']}, UUID {ident['uuid']}")

    def set_state(self, connected: bool, busy: bool, config_ok: bool) -> None:
        self.term_button.setEnabled(connected and not busy)
        self.repl_button.setEnabled(connected and not busy and config_ok)
        self.repl_input.setEnabled(connected and config_ok)
