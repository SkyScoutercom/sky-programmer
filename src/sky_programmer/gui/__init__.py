"""The SKY programmer desktop app. A front end only: every board action goes through Programmer."""
from __future__ import annotations


def main(argv=None) -> int:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("The desktop app needs PySide6. Install it with: pipx install --force '.[gui]'")
        return 1
    from .app import run

    return run(argv)
