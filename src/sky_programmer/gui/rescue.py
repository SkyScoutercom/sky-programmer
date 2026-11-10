"""Rescuing a board that a native library keeps rebooting, from the desktop app (`sky code rescue`).

It runs with no board connected: the board drops off USB at every reset, so the rescue opens its own connection each
time the board comes back, and erases the stored script inside the start-up guard's window.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout

from .. import code
from ..errors import SkyError

RESCUE_TEXT = ("Use this when a native library makes the board reboot over and over. It waits for the board to power "
               "up, backs up its script, then erases it, so the board starts with no script. It works for scripts "
               "stored by the SKY programmer, which wait 10 s at a power-up with the motor still. A script stored by "
               "VESC Tool has no such wait: then the HALL2-SWCLK jumper or an SWD programmer is needed (SAFETY.md). "
               "Your script backups stay on this computer.")  # fmt: skip


class RescueThread(QThread):
    message = Signal(str)
    done = Signal()  # the outcome stays on the Python side: result or error

    def __init__(self, open_link, find_ports, backup_dir, parent=None):
        super().__init__(parent)
        self.open_link, self.find_ports, self.backup_dir = open_link, find_ports, backup_dir
        self.result, self.error, self._stop = None, None, False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            self.result = code.rescue(self.open_link, self.find_ports, self.backup_dir, say=self.message.emit,
                                      should_stop=lambda: self._stop)  # fmt: skip
        except SkyError as e:
            self.error = e
        except Exception as e:  # a device that isn't a VESC can answer with anything
            self.error = SkyError(f"unexpected problem: {e!r}")
        self.done.emit()


class RescueDialog(QDialog):
    """Shows what the rescue is doing, with a Cancel that works until the erase starts."""

    def __init__(self, parent, thread: RescueThread):
        super().__init__(parent)
        self.thread = thread
        self.setWindowTitle("Rescue")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        intro = QLabel(f"<b>Power-cycle the board now</b>, with the engine stopped. Waiting up to "
                       f"{code.RESCUE_WAIT_S:g} s for it to come up.")  # fmt: skip
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        lay.addWidget(self.log)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.cancel)
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        lay.addWidget(buttons)
        thread.message.connect(self.log.appendPlainText)

    def cancel(self) -> None:
        self.thread.stop()
        self.cancel_button.setEnabled(False)
        self.log.appendPlainText("Cancelling: nothing is erased unless the erase has already started.")

    def reject(self) -> None:  # Esc and the window's close button cancel too, but the dialog waits for the thread
        self.cancel()
