"""Status, pull and reset for the app's own source folder, like the SKY kiosk's OTA pane. Runs off the GUI thread."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import gitops


class _GitJob(QThread):
    done = Signal(object, object)

    def __init__(self, fn, parent):
        super().__init__(parent)
        self.fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self.fn(), None)
        except Exception as e:  # e.g. git isn't installed: the page must still say something
            self.done.emit(None, e)


class UpdatesPage(QWidget):
    def __init__(self, win, repo_dir=None):
        super().__init__()
        self.win = win
        self.repo = Path(repo_dir) if repo_dir else None
        self._job = None
        self._connected = False
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        bar = QHBoxLayout()
        title = QLabel("Updates")
        title.setObjectName("PageTitle")
        self.status_button = QPushButton("Status")
        self.status_button.clicked.connect(self.refresh)
        self.reset_button = QPushButton("Reset…")
        self.reset_button.setObjectName("Danger")
        self.reset_button.clicked.connect(self.reset_folder)
        self.pull_button = QPushButton("Pull")
        self.pull_button.setObjectName("Primary")
        self.pull_button.clicked.connect(self.pull)
        bar.addWidget(title)
        bar.addStretch(1)
        for b in (self.status_button, self.reset_button, self.pull_button):
            bar.addWidget(b)
        root.addLayout(bar)

        card = QFrame()
        card.setObjectName("Card")
        col = QVBoxLayout(card)
        col.setContentsMargins(16, 12, 16, 12)
        row = QHBoxLayout()
        self.folder_label = QLabel()
        self.folder_label.setWordWrap(True)
        self.change_button = QPushButton("Change folder…")
        self.change_button.clicked.connect(self.choose_folder)
        row.addWidget(self.folder_label, 1)
        row.addWidget(self.change_button)
        col.addLayout(row)
        self.hint = QLabel()
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        col.addWidget(self.hint)
        root.addWidget(card)

        self.output = QPlainTextEdit()
        self.output.setObjectName("Output")
        self.output.setReadOnly(True)
        root.addWidget(self.output, 1)
        self._show_folder()
        self._sync()

    # ---------------------------------------------------------------- the folder
    def set_repo(self, path) -> None:
        self.repo = Path(path) if path else None
        self._show_folder()
        self._sync()

    def choose_folder(self) -> None:
        chosen = self.win.prompts.ask_folder(str(self.repo or Path.home()))
        if not chosen:
            return
        if not gitops.is_app_repo(chosen):
            self.win.prompts.error("Not this project", f"{chosen} is not a checkout of SKY programmer. A reset there "
                                   "could destroy someone's work, so it can't be used here.")  # fmt: skip
            return
        self.win.remember_repo(chosen)
        self.set_repo(chosen)
        self.refresh()

    def _show_folder(self) -> None:
        if not self._usable():
            self.folder_label.setText("<b>Source folder:</b> not found. Choose your sky-programmer checkout.")
            return
        self.folder_label.setText(f"<b>Source folder:</b> {self.repo}")

    def _usable(self) -> bool:
        return self.repo is not None and gitops.is_app_repo(self.repo)

    # ---------------------------------------------------------------- operations
    @property
    def busy(self) -> bool:
        return self._job is not None

    def _start(self, fn, done) -> bool:
        if self.busy:
            return False
        job = _GitJob(fn, self)
        self._job = job

        def finished(result, error):
            job.wait(5000)
            job.deleteLater()
            self._job = None
            self._sync()
            if error is not None:
                self.output.setPlainText(f"Could not run git: {error}")
                return
            done(result)

        job.done.connect(finished)
        job.start()
        self._sync()
        return True

    def refresh(self) -> None:
        if not self._usable():
            self.output.setPlainText("No source folder chosen yet.")
            return
        repo = self.repo
        self.output.setPlainText("Reading the folder…")
        self._start(lambda: gitops.report(repo), self.output.setPlainText)

    def pull(self) -> None:
        if not self._may_change_code():
            return
        repo = self.repo
        self.output.setPlainText("Pulling from GitHub…")
        self._start(lambda: gitops.pull(repo), self._finished)

    def reset_folder(self) -> None:
        if not self._may_change_code():
            return
        repo = self.repo
        self.output.setPlainText("Checking what a reset would change…")
        self._start(lambda: gitops.reset_preview(repo), self._review_reset)

    def _review_reset(self, preview: dict) -> None:
        if not preview["ok"]:
            self.output.setPlainText(preview["output"])
            return
        if not self.win.prompts.confirm_reset(preview):
            self.output.setPlainText("Cancelled. Nothing was reset.")
            return
        if not self._may_change_code():  # a board may have been connected while the dialog was open
            return
        repo, commit, target = self.repo, preview["commit"], preview["target"]
        self._start(lambda: gitops.reset_hard(repo, commit, target), self._finished)

    def _finished(self, result: dict) -> None:
        self.output.setPlainText(result["output"] or "Done.")
        self.win.log(f"{result['op']}: {'done' if result['ok'] else 'failed'}")
        if result["changed"]:
            self.win.code_changed(gitops.running_from(self.repo), self.repo)

    def _may_change_code(self) -> bool:
        """No pull or reset with a board connected, so code is never swapped underneath a session."""
        if self._connected:
            self.win.prompts.info("Disconnect first", "Disconnect from the board before updating.")
            return False
        return self._usable()

    # ---------------------------------------------------------------- state
    def _sync(self) -> None:
        usable = self._usable() and not self.busy
        self.status_button.setEnabled(usable)
        self.pull_button.setEnabled(usable and not self._connected)
        self.reset_button.setEnabled(usable and not self._connected)
        self.change_button.setEnabled(not self.busy and not self._connected)
        if self._connected:
            self.hint.setText("Disconnect from the board to pull or reset. Status still works.")
        elif self._usable() and not gitops.running_from(self.repo):
            self.hint.setText("This app is an installed copy, so a pull updates this folder but not the app. To use "
                              f"the new code, run: pipx install --force --editable '{self.repo}[gui]'")  # fmt: skip
        else:
            self.hint.setText("Pull only moves forward: if this folder and GitHub have both changed, nothing is "
                              "merged. Reset makes this folder match GitHub and discards changes here.")  # fmt: skip

    def set_state(self, connected: bool, busy: bool, config_ok: bool) -> None:
        self._connected = connected or self.win.board is not None
        self._sync()

