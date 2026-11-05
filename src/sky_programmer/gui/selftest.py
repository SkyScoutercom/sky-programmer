"""`--self-test`: prove this install has what its safety checks depend on, then run a write on a simulated board.

A packaged build can leave data files behind without any error at build time, and the REPL's word filter and the config
layouts are data files. The Windows build runs this on the finished .exe and fails if it doesn't pass. It touches no
hardware and writes only to temporary folders.
"""
from __future__ import annotations

import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from .. import config, safety
from ..errors import SafetyRefusal
from ..link import list_ports
from . import theme
from .demo import DemoBoard
from .window import MainWindow


class _Answers:
    """Says yes to everything, and remembers errors so they can fail the test."""

    def __init__(self):
        self.errors = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: True

    def error(self, title, text):
        self.errors.append(f"{title}: {text}")

    def info(self, title, text):
        pass


def _wait_until(app, condition, what: str, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def _refuses(text: str) -> bool:
    try:
        safety.check_repl(text)
    except SafetyRefusal:
        return True
    return False


def _check_data_files() -> list:
    results = [("config layout for firmware 7.00", len(config.load_layout(7, 0).fields[config.MC_ROOT]) > 100),
               ("firmware word list", len(safety.firmware_words()) > 100),
               ("REPL refuses cmds-proc", _refuses("(cmds-proc 73)")),
               ("REPL refuses conf-store", _refuses("(conf-store)")),
               ("REPL refuses motor words", _refuses("(set-current 5)")),
               ("logo", not theme.logo_pixmap(36).isNull()),
               ("app icon", not theme.icon_image(64).isNull())]  # fmt: skip
    for palette in (theme.DAY, theme.NIGHT):
        for path in re.findall(r'url\("([^"]+)"\)', theme.qss(palette)):
            results.append((f"image {Path(path).name}", not QImage(path).isNull()))
    list_ports()  # must not raise, even with no board
    results.append(("listing serial ports", True))
    return results


def _write_on_a_simulated_board(app) -> list:
    demo, prompts = DemoBoard(), _Answers()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        win = MainWindow(backup_dir=folder, demo=demo, prompts=prompts, settings_path=Path(folder) / "settings.ini",
                         repo_dir=folder)  # fmt: skip
        win.show()
        _wait_until(app, lambda: win.info is not None, "the simulated board to connect")
        _wait_until(app, lambda: win.dashboard.tiles["bus"].value.text() != "—", "live values")
        _wait_until(app, lambda: "l_temp_fet_start" in win.config_page.motor.rows, "the config to load")
        win.backups_page.backup_now()
        _wait_until(app, lambda: win.backups_page.table.rowCount() >= 1, "a backup")
        _wait_until(app, lambda: not win.board.busy, "the backup to finish")
        win.config_page.motor.set_value("l_temp_fet_start", "84")
        win.config_page.review()
        _wait_until(app, lambda: demo.value("l_temp_fet_start") == 84, "the write")
        _wait_until(app, lambda: not win.board.busy, "the write to finish")
        wrote_once = len(demo.written) == 1
        win.close()
    return [("connect, read, back up and write on a simulated board", wrote_once),
            ("no unexpected error dialogs", not prompts.errors)]


def run_self_test(report_path=None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    lines = []
    try:
        results = _check_data_files() + _write_on_a_simulated_board(app)
        lines = [f"{'ok  ' if ok else 'FAIL'} {name}" for name, ok in results]
        passed = all(ok for _, ok in results)
    except Exception:
        lines.append("FAIL " + traceback.format_exc())
        passed = False
    text = "\n".join(lines + ["PASSED" if passed else "FAILED"]) + "\n"
    if report_path:
        Path(report_path).write_text(text, encoding="utf-8")
    print(text)
    return 0 if passed else 1
