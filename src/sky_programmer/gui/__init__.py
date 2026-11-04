"""The SKY programmer desktop app. A front end only: every board action goes through Programmer."""
from __future__ import annotations


def main(argv=None) -> int:
    try:
        from .app import run
    except ImportError as e:  # PySide6 itself, or a system library it needs
        print(f"The desktop app can't start: {e}\nInstall it with: pipx install --force --editable '.[gui]'")
        return 1
    return run(argv)
