"""Where this app keeps its files on each operating system."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR = "sky-programmer"


def data_dir() -> Path:
    """Backups and logs."""
    base = os.environ.get("XDG_DATA_HOME")
    if not base and sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base or Path.home() / ".local" / "share") / APP_DIR


def config_dir() -> Path:
    """Settings such as the day or night theme."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base and sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base or Path.home() / ".config") / APP_DIR
