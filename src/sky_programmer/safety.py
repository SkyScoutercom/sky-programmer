"""The board-protection allow-list. Every packet the app sends passes check_packet() first.

It is an allow-list, so a command nobody has reviewed is refused by default. SAFETY.md explains
each rule with the firmware code behind it.

This protects against the app and its operator making mistakes. It is not a security boundary: a
determined user can still get around the REPL word filter, for example by building a symbol name
at runtime. The commands that can kill a board's firmware are refused outright.
"""
from __future__ import annotations

import re

from . import protocol as p
from .errors import SafetyRefusal

_READ_ONLY = {
    p.COMM_FW_VERSION: "FW_VERSION",
    p.COMM_GET_VALUES: "GET_VALUES",
    p.COMM_GET_MCCONF: "GET_MCCONF",
    p.COMM_GET_MCCONF_DEFAULT: "GET_MCCONF_DEFAULT",
    p.COMM_GET_APPCONF: "GET_APPCONF",
    p.COMM_LISP_GET_STATS: "LISP_GET_STATS",
}

_CONDITIONAL = {
    p.COMM_SET_CURRENT: "SET_CURRENT (zero only)",
    p.COMM_SET_MCCONF: "SET_MCCONF (only through the verified write path)",
    p.COMM_TERMINAL_CMD: "TERMINAL_CMD (read-only commands only)",
    p.COMM_LISP_SET_RUNNING: "LISP_SET_RUNNING",
    p.COMM_LISP_REPL_CMD: "LISP_REPL_CMD (filtered)",
}

ALLOWED = {**_READ_ONLY, **_CONDITIONAL}

_FIRMWARE = "changes the firmware image; a bad image or interrupted flash can need SWD to recover"
_BOOTLOADER = "touches the bootloader; without one the board cannot be updated over USB and may not recover"
_BRIDGE = "drives the SWD bridge that flashes other chips"
_FORWARD = "would run a command on another board, out of reach of these checks"

# Named so the refusal can say why. Anything else not in ALLOWED is refused as well.
FORBIDDEN = {
    1: ("JUMP_TO_BOOTLOADER", _BOOTLOADER),
    2: ("ERASE_NEW_APP", _FIRMWARE),
    3: ("WRITE_NEW_APP_DATA", _FIRMWARE),
    34: ("FORWARD_CAN", _FORWARD),
    59: ("JUMP_TO_BOOTLOADER_ALL_CAN", _BOOTLOADER),
    60: ("ERASE_NEW_APP_ALL_CAN", _FIRMWARE),
    61: ("WRITE_NEW_APP_DATA_ALL_CAN", _FIRMWARE),
    66: ("BM_CONNECT", _BRIDGE),
    67: ("BM_ERASE_FLASH_ALL", _BRIDGE),
    68: ("BM_WRITE_FLASH", _BRIDGE),
    69: ("BM_REBOOT", _BRIDGE),
    73: ("ERASE_BOOTLOADER", _BOOTLOADER),
    74: ("ERASE_BOOTLOADER_ALL_CAN", _BOOTLOADER),
    81: ("WRITE_NEW_APP_DATA_LZO", _FIRMWARE),
    82: ("WRITE_NEW_APP_DATA_ALL_CAN_LZO", _FIRMWARE),
    83: ("BM_WRITE_FLASH_LZO", _BRIDGE),
    85: ("CAN_FWD_FRAME", _FORWARD),
    102: ("JUMP_TO_BOOTLOADER_HW", _BOOTLOADER),
    103: ("ERASE_NEW_APP_HW", _FIRMWARE),
    104: ("WRITE_NEW_APP_DATA_HW", _FIRMWARE),
    105: ("ERASE_BOOTLOADER_HW", _BOOTLOADER),
    106: ("JUMP_TO_BOOTLOADER_ALL_CAN_HW", _BOOTLOADER),
    107: ("ERASE_NEW_APP_ALL_CAN_HW", _FIRMWARE),
    108: ("WRITE_NEW_APP_DATA_ALL_CAN_HW", _FIRMWARE),
    109: ("ERASE_BOOTLOADER_ALL_CAN_HW", _BOOTLOADER),
    125: ("BM_MEM_WRITE", _BRIDGE),
}

# Terminal commands that only report. The others spin the motor, write config or reboot.
TERMINAL_READ_ONLY = {
    "help", "fault", "faults", "volt", "mem", "threads", "hw_status", "foc_state", "uptime", "crc",
    "fw_info", "tim",
}  # fmt: skip

# Words a REPL line may not contain. Flash and persistence first, then the ways to run hidden code.
REPL_FORBIDDEN = {
    "conf-store", "store-backup", "conf-restore-mc", "conf-restore-app",
    "load-native-lib", "unload-native-lib", "shutdown", "can-cmd", "canmsg-send",
    "read-eval", "read-eval-program", "import",
    "lbm-erase", "lbm-write", "lbm-run", "qml-erase", "qml-write",
}  # fmt: skip
REPL_FORBIDDEN_PREFIXES = ("fw-", "bm-")

# REPL words that can move the motor or change limits in RAM: refused unless asked for.
REPL_MOTOR = {
    "set-current", "set-current-rel", "set-duty", "set-rpm", "set-pos", "set-brake", "set-brake-rel",
    "set-handbrake", "set-handbrake-rel", "set-kill-sw", "set-servo", "set-aux", "foc-openloop",
    "foc-openloop-phase", "foc-beep",
    "foc-play-tone", "foc-play-samples", "conf-set", "conf-measure-res", "conf-measure-ind",
    "conf-detect-foc", "conf-detect-hall", "conf-detect-lambda-enc", "conf-dc-cal", "conf-dc-cal-set",
    "conf-enc-sincos", "conf-remap-as504x", "conf-remap-hall", "conf-set-pid-offset", "app-adc-override",
    "app-ppm-override", "pwm-set-duty",
}  # fmt: skip

# REPL meta commands that restart or resume a stored script: `sky start` is the way to do that.
REPL_META_FORBIDDEN = (":reset", ":continue")

# The firmware's REPL truncates long input, and a truncated expression can mean something else.
REPL_MAX_LEN = 300

_TOKEN = re.compile(r"[^\s()\[\]'`,\"]+")


def check_repl(text: str, *, allow_motor: bool = False) -> None:
    line = text.strip()
    if len(text) > REPL_MAX_LEN:
        raise SafetyRefusal(f"REPL input is {len(text)} characters; the firmware truncates long input. "
                            f"Keep it under {REPL_MAX_LEN} and split it into several lines.")
    if line.lower().startswith(REPL_META_FORBIDDEN):
        raise SafetyRefusal(f"{line.split()[0]} restarts or resumes the stored script. Use `sky start`.")
    for word in (t.lower() for t in _TOKEN.findall(text)):
        if word in REPL_FORBIDDEN or word.startswith(REPL_FORBIDDEN_PREFIXES):
            raise SafetyRefusal(f"`{word}` is blocked in the REPL because it writes flash or runs hidden code. "
                                "Use `sky set` and `sky restore` for config.")
        if word in REPL_MOTOR and not allow_motor:
            raise SafetyRefusal(f"`{word}` can move the motor or change limits. "
                                "Run `sky repl --allow-motor` if you mean it.")


def check_terminal(text: str) -> None:
    words = text.split()
    if len(words) != 1 or words[0] not in TERMINAL_READ_ONLY:
        raise SafetyRefusal(f"terminal command `{text}` is not allowed. "
                            f"Allowed: {', '.join(sorted(TERMINAL_READ_ONLY))}")


def check_packet(payload: bytes, *, config_write: bool = False, repl_allow_motor: bool = False) -> None:
    """Raise SafetyRefusal unless this exact packet is allowed to leave the app."""
    if not payload:
        raise SafetyRefusal("empty packet")
    cmd = payload[0]
    if cmd in FORBIDDEN:
        name, why = FORBIDDEN[cmd]
        raise SafetyRefusal(f"COMM_{name} ({cmd}) is permanently blocked: it {why}.")
    if cmd not in ALLOWED:
        raise SafetyRefusal(f"command {cmd} is not on the allow-list.")
    if cmd in _READ_ONLY:
        if len(payload) != 1:
            raise SafetyRefusal(f"COMM_{ALLOWED[cmd]} must not carry data.")
    elif cmd == p.COMM_SET_MCCONF:
        if not config_write:
            raise SafetyRefusal("config writes must go through the verified write path.")
    elif cmd == p.COMM_SET_CURRENT:
        if payload != bytes([p.COMM_SET_CURRENT, 0, 0, 0, 0]):
            raise SafetyRefusal("only a zero-current command is allowed; it releases the motor.")
    elif cmd == p.COMM_LISP_SET_RUNNING:
        if payload[1:] not in (b"\x00", b"\x01"):
            raise SafetyRefusal("LISP_SET_RUNNING takes one byte, 0 or 1.")
    elif cmd == p.COMM_TERMINAL_CMD:
        check_terminal(_text(payload))
    elif cmd == p.COMM_LISP_REPL_CMD:
        check_repl(_text(payload), allow_motor=repl_allow_motor)


def _text(payload: bytes) -> str:
    return payload[1:].rstrip(b"\0").decode(errors="replace")
