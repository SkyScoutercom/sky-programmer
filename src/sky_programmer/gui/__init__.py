"""The SKY programmer desktop app. A front end only: every board action goes through Programmer."""
from __future__ import annotations

import sys


def main(argv=None) -> int:
    if sys.version_info < (3, 12):  # measured on PySide6 6.12: any signal sent from a thread corrupts memory below 3.12
        print("The desktop app needs Python 3.12 or newer, because its GUI library crashes on older versions. "
              "The `sky` command works on this Python.")
        return 1
    try:
        from .app import run
    except ImportError as e:  # PySide6 itself, or a system library it needs
        print(f"The desktop app can't start: {e}\nInstall it with: pipx install --force --editable '.[gui]'")
        return 1
    return run(argv)
