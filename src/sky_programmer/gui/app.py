"""Start the desktop app, or install its Ubuntu launcher."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from .. import APP_NAME, __version__
from . import theme
from .demo import DemoBoard
from .window import MainWindow

DESKTOP_ID = "sky-programmer"


def _desktop_quote(path: str) -> str:
    """Quote a path for an Exec line, as the desktop entry format requires."""
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + escaped.replace("%", "%%") + '"'


def install_launcher() -> Path:
    """Add SKY programmer to the Ubuntu app grid, for this user only."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    icon = data / "icons/hicolor/256x256/apps" / f"{DESKTOP_ID}.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    if not theme.icon_image(256).save(str(icon)):
        raise OSError(f"could not save {icon}")
    # GNOME Shell only rescans when this folder's own time changes, which adding a file deeper inside doesn't do.
    os.utime(data / "icons/hicolor")
    entry = data / "applications" / f"{DESKTOP_ID}.desktop"
    entry.parent.mkdir(parents=True, exist_ok=True)
    # This install's own Python, so the launcher can't pick up a different copy found on PATH. The icon's full path,
    # because GNOME Shell can miss an icon added to the theme folder after it started.
    entry.write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={APP_NAME}\nComment=Read and configure VESC motor controllers\n"
        f"Exec={_desktop_quote(sys.executable)} -m sky_programmer.gui\nIcon={icon}\nTerminal=false\n"
        f"Categories=Development;Electronics;\nStartupWMClass={DESKTOP_ID}\n")
    return entry


def _set_windows_app_id() -> None:
    """So the Windows taskbar shows this app's own icon, rather than grouping it under Python's."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SkyScouter.SKYprogrammer")


def _show_unexpected_errors(window) -> None:
    """Under the launcher there's no terminal, so an unexpected error would otherwise vanish."""

    def hook(kind, value, tb) -> None:
        traceback.print_exception(kind, value, tb)
        try:
            window.log(f"Unexpected error: {value!r}")
            QMessageBox.critical(window, "Unexpected error", f"Please report this.\n\n{kind.__name__}: {value}")
        except Exception:
            pass

    sys.excepthook = hook


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sky-gui", description=f"{APP_NAME} desktop app.")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    ap.add_argument("--port", default=os.environ.get("SKY_PORT"), help="start with this serial port chosen")
    ap.add_argument("--backup-dir", default=None, help="where config backups are kept")
    ap.add_argument("--simulate", action="store_true", help="try the app on a simulated board; no hardware is touched")
    ap.add_argument("--install-launcher", action="store_true", help="add the app to the Ubuntu app grid, then exit")
    ap.add_argument("--self-test", nargs="?", const="", metavar="REPORT",
                    help="check this install, using a simulated board, and exit: 0 means it passed")  # fmt: skip
    args = ap.parse_args(argv)

    if args.install_launcher:
        if sys.platform == "win32":
            print("On Windows, the installer adds the Start menu entry.")
            return 1
        print(f"Launcher installed: {install_launcher()}")
        return 0
    if args.self_test is not None:
        from .selftest import run_self_test

        return run_self_test(args.self_test)

    _set_windows_app_id()

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(DESKTOP_ID)
    app.setStyle("Fusion")
    app.setWindowIcon(theme.app_icon())

    if args.simulate:  # simulated backups must never land next to a real board's
        with tempfile.TemporaryDirectory(prefix="sky-simulated-") as folder:
            win = MainWindow(backup_dir=folder, demo=DemoBoard())
            _show_unexpected_errors(win)
            win.show()
            return app.exec()
    win = MainWindow(backup_dir=args.backup_dir, preset_port=args.port)
    _show_unexpected_errors(win)
    win.show()
    return app.exec()
