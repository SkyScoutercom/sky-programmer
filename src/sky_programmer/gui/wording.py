"""The engine's refusals name terminal commands. Here they name the buttons instead, and say what isn't offered."""
from __future__ import annotations

import re

_COMMANDS = (
    (r"`sky backup`", "Back up now"),
    (r"`sky stop`", "Pause script"),
    (r"`sky start`", "Start script"),
    (r"`sky status`", "reconnect"),
    (r"`sky diff [^`]*`", "Compare on the Backups page"),
    (r"`sky restore [^`]*`", "Restore on the Backups page"),
)

NO_OVERRIDE = ("This desktop app doesn't offer that override. If you really mean it, disconnect here and use the `sky` "
               "command in a terminal.")


def humanize(text: str) -> str:
    for pattern, words in _COMMANDS:
        text = re.sub(pattern, words, text)
    if re.search(r"(?<!\w)--[a-z]", text):
        text += "\n\n" + NO_OVERRIDE
    return text
