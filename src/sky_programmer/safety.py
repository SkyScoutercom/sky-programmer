"""The board-protection allow-list. Every packet the app sends passes check_packet() first.

It is an allow-list, so a command nobody has reviewed is refused by default. SAFETY.md explains
each rule with the firmware code behind it.

This protects against the app and its operator making mistakes. It is not a security boundary: a
function defined by a script on the board runs whatever that script says, and the REPL can't see
inside it. The packets that can kill a board's firmware are refused outright.
"""
from __future__ import annotations

import json
import re
import struct
from functools import lru_cache
from importlib import resources

from . import protocol as p
from .errors import SafetyRefusal

_READ_ONLY = {
    p.COMM_FW_VERSION: "FW_VERSION",
    p.COMM_GET_VALUES: "GET_VALUES",
    p.COMM_GET_MCCONF: "GET_MCCONF",
    p.COMM_GET_MCCONF_DEFAULT: "GET_MCCONF_DEFAULT",
    p.COMM_GET_APPCONF: "GET_APPCONF",
}

_CONDITIONAL = {
    p.COMM_MOTOR_ESTOP: "MOTOR_ESTOP (one fixed packet)",
    p.COMM_SET_MCCONF: "SET_MCCONF (only through the verified write path)",
    p.COMM_TERMINAL_CMD: "TERMINAL_CMD (read-only commands only)",
    p.COMM_LISP_SET_RUNNING: "LISP_SET_RUNNING",
    p.COMM_LISP_REPL_CMD: "LISP_REPL_CMD (filtered)",
    p.COMM_LISP_READ_CODE: "LISP_READ_CODE (length 1-400, offset 0 or more)",
    p.COMM_LISP_WRITE_CODE: "LISP_WRITE_CODE (only through the script upload path, inside the code area)",
    p.COMM_LISP_ERASE_CODE: "LISP_ERASE_CODE (only through the script upload path)",
    p.COMM_LISP_STREAM_CODE: "LISP_STREAM_CODE (only through `sky code try`, mode 1)",
}

# The Lisp code area is one 128 kB flash sector. The firmware writes to its base + offset with no bounds check
# (flash_helper.c), and the next sector is the bootloader, so every write is checked here instead.
CODE_AREA_BYTES = 128 * 1024
CODE_READ_MAX = 400

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

# COMM_MOTOR_ESTOP releases the motor and ignores input for this long: 1000 ms, big-endian.
MOTOR_RELEASE_PACKET = bytes([p.COMM_MOTOR_ESTOP, 0x03, 0xE8])

# LispBM words that read or print, reviewed against the firmware 7.00 list.
REPL_READ_PREFIXES = ("get-", "canget-", "gnss-", "raw-", "phase-", "ioboard-get-", "encoder-")
REPL_READ = {
    "print", "puts", "crc16", "crc32", "bits-dec-int", "bits-enc-int", "buf-resize", "rand", "rand-max", "secs-since",
    "systime", "sysinfo", "stats", "conf-get", "conf-get-limits", "eeprom-read-f", "eeprom-read-i", "gpio-read",
    "observer-error", "pos-pid-error", "pos-pid-now", "icu-period", "icu-width", "app-is-output-disabled",
    "app-adc-range-ok", "app-pas-get-rpm", "can-local-id", "can-msg-age", "shutdown-btn-read", "uavcan-last-rawcmd",
    "uavcan-last-rpmcmd", "prof-result", "throttle-curve",
}  # fmt: skip

# Words that can move the motor or servo, or change limits in RAM: `sky repl --allow-motor` only.
REPL_MOTOR = {
    "set-current", "set-current-rel", "set-duty", "set-rpm", "set-pos", "set-brake", "set-brake-rel",
    "set-handbrake", "set-handbrake-rel", "set-kill-sw", "set-servo", "set-aux", "foc-openloop",
    "foc-openloop-phase", "foc-beep", "foc-play-tone", "foc-play-samples", "foc-play-stop", "conf-set",
    "conf-measure-res", "conf-measure-ind", "conf-detect-foc", "conf-detect-hall", "conf-detect-lambda-enc",
    "conf-dc-cal", "conf-dc-cal-set", "app-adc-override", "app-ppm-override", "app-adc-detach", "app-ppm-detach",
    "app-disable-output", "pwm-start", "pwm-stop", "pwm-set-duty", "gpio-write", "pos-pid-set", "timeout-reset",
    "override-temp-motor", "override-speed",
}  # fmt: skip

# Core language words that evaluate text or write Lisp storage. They aren't in the firmware's extension list.
REPL_CORE_FORBIDDEN = {
    "read", "read-program", "read-eval", "read-eval-program", "eval", "eval-program", "move-to-flash", "import",
    "load-native-lib", "unload-native-lib", "fw-erase", "fw-write", "fw-reboot", "lbm-erase", "lbm-write", "lbm-run",
    "qml-erase", "qml-write",
    # These build a word from data at run time, and `apply` calls it, so `(apply (str2sym ...))` could reach any
    # blocked word without naming it (lispBM eval_cps.c, apply on a symbol).
    "str2sym", "u2sym", "unflatten", "apply",
}  # fmt: skip
REPL_FORBIDDEN_PREFIXES = ("fw-", "bm-", "@")

# Meta commands that only report. The rest pause, resume or restart the script, or undefine symbols.
REPL_META_ALLOWED = {":help", ":info", ":ctxs", ":verb"}

# The firmware's REPL truncates long input, and a truncated expression can mean something else.
REPL_MAX_LEN = 300

_TOKEN = re.compile(r"[^\s()\[\]{}'`,\"]+")


@lru_cache(maxsize=1)
def firmware_words() -> frozenset:
    """Every LispBM word the supported firmware adds. One not reviewed above is refused."""
    words = set()
    for entry in resources.files("sky_programmer.layouts").iterdir():
        if entry.name.endswith(".json"):
            words |= set(json.loads(entry.read_text())["lisp_extensions"])
    if not words:  # an empty list would let every firmware word through, so a broken install must fail closed
        raise SafetyRefusal("this install is missing its firmware word list, so the REPL is disabled. Reinstall it.")
    return frozenset(words)


def _repl_word_is_read_only(word: str) -> bool:
    return word in REPL_READ or word.startswith(REPL_READ_PREFIXES)


def check_repl(text: str, *, allow_motor: bool = False) -> None:
    line = text.strip()
    if len(text) > REPL_MAX_LEN:
        raise SafetyRefusal(f"REPL input is {len(text)} characters; the firmware truncates long input. "
                            f"Keep it under {REPL_MAX_LEN} and split it into several lines.")
    if line.startswith(":"):
        word = line.split()[0].lower()
        if word not in REPL_META_ALLOWED:
            raise SafetyRefusal(f"{word} can freeze, resume or change the running script. "
                                f"Allowed: {', '.join(sorted(REPL_META_ALLOWED))}. Use `sky stop` and `sky start`.")
        return
    if ";" in text or "\\" in text:
        # LispBM ends a symbol at ';' and strings can hide text behind escapes, so `(conf-store;x` would slip past.
        raise SafetyRefusal("comments and backslashes aren't allowed in the REPL.")
    for word in (t.lower() for t in _TOKEN.findall(text)):
        if word in REPL_CORE_FORBIDDEN or word.startswith(REPL_FORBIDDEN_PREFIXES):
            raise SafetyRefusal(f"`{word}` is blocked in the REPL because it writes flash or runs hidden code.")
        if word in REPL_MOTOR:
            if not allow_motor:
                raise SafetyRefusal(f"`{word}` can move the motor or servo, or change limits. "
                                    "Run `sky repl --allow-motor` if you mean it.")
        elif word in firmware_words() and not _repl_word_is_read_only(word):
            raise SafetyRefusal(f"`{word}` is blocked in the REPL because it can write flash, reach other boards "
                                "or change the board's state.")


def check_terminal(text: str) -> None:
    words = text.split()
    if len(words) != 1 or words[0] not in TERMINAL_READ_ONLY:
        raise SafetyRefusal(f"terminal command `{text}` is not allowed. "
                            f"Allowed: {', '.join(sorted(TERMINAL_READ_ONLY))}")


def check_code_packet(payload: bytes, *, code_write: bool, code_stream: bool) -> None:
    """The Lisp code commands, whose bounds the firmware doesn't check (docs/LISP_UPLOAD_SPEC.md, 1.2 to 1.6)."""
    cmd = payload[0]
    if cmd == p.COMM_LISP_READ_CODE:
        if len(payload) != 9:
            raise SafetyRefusal("LISP_READ_CODE takes a length and an offset.")
        length, offset = struct.unpack_from(">ii", payload, 1)
        if not 1 <= length <= CODE_READ_MAX or offset < 0 or offset + length > CODE_AREA_BYTES:
            raise SafetyRefusal(f"LISP_READ_CODE length {length} at offset {offset} is outside the code area.")
    elif cmd == p.COMM_LISP_ERASE_CODE:
        if not code_write:
            raise SafetyRefusal("erasing the script area must go through the script upload path.")
        if len(payload) != 5:
            raise SafetyRefusal("LISP_ERASE_CODE takes exactly one 4-byte size.")
    elif cmd == p.COMM_LISP_WRITE_CODE:
        if not code_write:
            raise SafetyRefusal("writing the script area must go through the script upload path.")
        if len(payload) < 6:  # the firmware turns 1-3 bytes of data into a 4 GB write across the bootloader
            raise SafetyRefusal("LISP_WRITE_CODE must carry an offset and at least one byte.")
        offset = struct.unpack_from(">I", payload, 1)[0]
        if offset + len(payload) - 5 > CODE_AREA_BYTES:
            raise SafetyRefusal(f"LISP_WRITE_CODE at offset {offset} would write past the code area, into the "
                                "bootloader. Refused.")  # fmt: skip
    elif cmd == p.COMM_LISP_STREAM_CODE:
        if not code_stream:
            raise SafetyRefusal("streaming a script must go through `sky code try`.")
        if len(payload) < 11:
            raise SafetyRefusal("LISP_STREAM_CODE must carry an offset, a length, a mode and some code.")
        offset, total, mode = struct.unpack_from(">iib", payload, 1)
        if mode != 1 or offset < 0 or offset + len(payload) - 10 > total:
            raise SafetyRefusal("LISP_STREAM_CODE must use mode 1 and stay inside the declared length.")


def check_packet(payload: bytes, *, config_write: bool = False, repl_allow_motor: bool = False,
                 code_write: bool = False, code_stream: bool = False) -> None:
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
    elif cmd == p.COMM_MOTOR_ESTOP:
        if payload != MOTOR_RELEASE_PACKET:
            raise SafetyRefusal("only the fixed motor-release packet is allowed.")
    elif cmd == p.COMM_LISP_SET_RUNNING:
        if payload[1:] not in (b"\x00", b"\x01"):
            raise SafetyRefusal("LISP_SET_RUNNING takes one byte, 0 or 1.")
    elif cmd == p.COMM_TERMINAL_CMD:
        check_terminal(_text(payload))
    elif cmd == p.COMM_LISP_REPL_CMD:
        check_repl(_text(payload), allow_motor=repl_allow_motor)
    elif cmd in (p.COMM_LISP_READ_CODE, p.COMM_LISP_WRITE_CODE, p.COMM_LISP_ERASE_CODE, p.COMM_LISP_STREAM_CODE):
        check_code_packet(payload, code_write=code_write, code_stream=code_stream)


def _text(payload: bytes) -> str:
    return payload[1:].rstrip(b"\0").decode(errors="replace")
