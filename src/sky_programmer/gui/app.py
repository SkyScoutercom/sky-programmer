"""Start the desktop app, or install its Ubuntu launcher."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .. import APP_NAME, __version__
from . import theme
from .demo import DemoBoard
from .window import MainWindow

DESKTOP_ID = "sky-programmer"


def install_launcher() -> Path:
    """Add SKY programmer to the Ubuntu app grid, for this user only."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    command = shutil.which("sky-gui") or f"{sys.executable} -m sky_programmer.gui"
    icon = data / "icons/hicolor/256x256/apps" / f"{DESKTOP_ID}.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    if not theme.icon_image(256).save(str(icon)):
        raise OSError(f"could not save {icon}")
    entry = data / "applications" / f"{DESKTOP_ID}.desktop"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={APP_NAME}\nComment=Read and configure VESC motor controllers\n"
        f"Exec={command}\nIcon={DESKTOP_ID}\nTerminal=false\n"
        f"Categories=Development;Electronics;\nStartupWMClass={DESKTOP_ID}\n")
    return entry


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sky-gui", description=f"{APP_NAME} desktop app.")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    ap.add_argument("--port", default=os.environ.get("SKY_PORT"), help="preselect this serial port")
    ap.add_argument("--backup-dir", default=None, help="where config backups are kept")
    ap.add_argument("--simulate", action="store_true", help="try the app on a simulated board; no hardware is touched")
    ap.add_argument("--install-launcher", action="store_true", help="add the app to the Ubuntu app grid, then exit")
    args = ap.parse_args(argv)

    if args.install_launcher:
        print(f"Launcher installed: {install_launcher()}")
        return 0

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(DESKTOP_ID)
    app.setStyle("Fusion")
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)  # the stylesheet is a light design, also on a dark desktop
    app.setStyleSheet(theme.QSS)
    app.setWindowIcon(theme.app_icon())

    if args.simulate:  # simulated backups must never land next to a real board's
        with tempfile.TemporaryDirectory(prefix="sky-simulated-") as folder:
            win = MainWindow(backup_dir=folder, demo=DemoBoard())
            win.show()
            return app.exec()
    win = MainWindow(backup_dir=args.backup_dir, preset_port=args.port)
    win.show()
    return app.exec()
