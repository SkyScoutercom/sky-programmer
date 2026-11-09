"""Back up, store and try LispBM scripts on the board (firmware 7.00).

Every firmware fact used here is cited in docs/LISP_UPLOAD_SPEC.md, and the rules are in docs/STAGE2_DESIGN.md. This
first step handles plain Lisp scripts only: a script that imports a file, such as a native library, is refused, except
when restoring this board's own backup.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import tempfile
import time
from pathlib import Path

from . import protocol as p
from .config import MC_ROOT
from .errors import CommsError, SafetyRefusal, VerifyFailed

CODE_AREA_BYTES = 128 * 1024  # flash sector 10
HEADER_BYTES = 8  # uint32 length, uint16 crc16 over flags + content, uint16 flags
MAX_CONTENT_BYTES = CODE_AREA_BYTES - HEADER_BYTES  # the firmware treats a longer length as no code
WRITE_CHUNK = 384  # as VESC Tool; 4 + 384 stays under the 512-byte packet limit
READ_CHUNK = 400
STREAM_CHUNK = 384
ERASE_TIMEOUT_S = 12.0  # VESC Tool waits 8 s; the firmware may wait 3 s per sector for the motor first
WRITE_TIMEOUT_S = 5.0  # each chunk may wait 3 s for the motor to release
READ_TIMEOUT_S = 1.5
STREAM_TIMEOUT_S = 4.0
STREAM_MODE_RESET_AND_LOAD = 1  # runs the streamed code alone; the stored script comes back at the next power-up

FORMAT = 1

# Words a script may not contain, and why. Matched as whole symbols outside comments and strings.
FORBIDDEN_WORDS = {
    "image-save": "it would make the board boot a saved `main` instead of the stored script",
    "cmds-proc": "it feeds raw packets to the firmware, including erasing the bootloader",
    "fw-erase": "it erases the firmware", "fw-write": "it writes the firmware", "fw-reboot": "it reboots the board",
    "lbm-erase": "it erases stored code", "lbm-write": "it writes stored code", "lbm-run": "it runs stored code",
    "qml-erase": "it erases stored code", "qml-write": "it writes stored code",
    "import": "imports, such as native libraries, aren't supported yet. Use VESC Tool for this script for now",
    "load-native-lib": "native libraries aren't supported yet. Use VESC Tool for this script for now",
}  # fmt: skip
# Words allowed but worth a warning.
WARNING_WORDS = {
    "conf-store": "it stores the motor config to flash, so the drift check can't tell its changes from yours",
    "eeprom-store-f": "it writes the board's EEPROM", "eeprom-store-i": "it writes the board's EEPROM",
}  # fmt: skip


# ---------------------------------------------------------------------- checking and packing a script
def symbols(text: str) -> list[str]:
    """The symbols in a LispBM script, ignoring comments, strings and character literals."""
    out, word, i = [], [], 0
    delimiters = set(" \t\r\n()[]{}'`,\"")

    def flush():
        if word:
            out.append("".join(word).lower())
            word.clear()

    while i < len(text):
        ch = text[i]
        if ch == ";":
            flush()
            while i < len(text) and text[i] != "\n":
                i += 1
        elif ch == '"':
            flush()
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "\\" and text[i:i + 2] == "\\#":  # character literal, e.g. \#a
            flush()
            i += 2
        elif ch in delimiters:
            flush()
        else:
            word.append(ch)
        i += 1
    flush()
    return out


def check_script(text: str) -> list[str]:
    """Refuse a script that would be unsafe to store or try. Returns warnings for words that are allowed."""
    if "\0" in text:
        raise SafetyRefusal("the script contains a NUL character, which would end it early on the board.")
    found = set(symbols(text))
    refused = sorted(found & set(FORBIDDEN_WORDS))
    if refused:
        raise SafetyRefusal("this script can't be uploaded: " + "; ".join(f"`{w}`: {FORBIDDEN_WORDS[w]}"
                                                                          for w in refused) + ".")  # fmt: skip
    return [f"`{w}`: {WARNING_WORDS[w]}." for w in sorted(found & set(WARNING_WORDS))]


def pack(text: str) -> bytes:
    """The stored content, as VESC Tool builds it with no imports: text, NUL, int16 import count 0."""
    content = text.encode("utf-8") + b"\0" + struct.pack(">h", 0)
    if len(content) > MAX_CONTENT_BYTES:
        raise SafetyRefusal(f"the script is {len(content)} bytes; the board holds at most {MAX_CONTENT_BYTES}.")
    return content


def image(content: bytes) -> bytes:
    """What is written to the code area: [uint32 N][uint16 crc16 over flags + content][uint16 flags = 0][content]."""
    if not 0 < len(content) <= MAX_CONTENT_BYTES:
        raise SafetyRefusal(f"stored code must be 1 to {MAX_CONTENT_BYTES} bytes, not {len(content)}.")
    flags = b"\0\0"
    return struct.pack(">IH", len(content), p.crc16(flags + content)) + flags + content


def import_count(content: bytes) -> int:
    """How many imported files, such as native libraries, the stored content carries."""
    end = content.find(b"\0")
    if end < 0 or len(content) < end + 3:
        return 0
    return struct.unpack_from(">h", content, end + 1)[0]


def script_text(content: bytes) -> str:
    end = content.find(b"\0")
    return content[: end if end >= 0 else len(content)].decode("utf-8", errors="replace")


# ---------------------------------------------------------------------- reading the stored code
def _read_chunk(link, length: int, offset: int) -> tuple:
    reply = link.request(bytes([p.COMM_LISP_READ_CODE]) + struct.pack(">ii", length, offset), timeout=READ_TIMEOUT_S)
    if len(reply) < 9:
        raise CommsError("the board's reply to reading the stored script was too short.")
    total, got = struct.unpack_from(">ii", reply, 1)
    return total, got, reply[9:]


def read_code(prog) -> bytes:
    """The stored content, or b"" if the board holds no valid script.

    A non-empty answer also proves the firmware's own CRC check of the whole stored code passed.
    """
    total, _, _ = _read_chunk(prog.link, 1, 0)  # 1 byte: the firmware doesn't answer a read past the end
    if total <= 0:
        return b""
    if total > MAX_CONTENT_BYTES:
        raise CommsError(f"the board reports {total} bytes of stored code, more than fits; not trusting it.")
    data = bytearray()
    while len(data) < total:
        want = min(READ_CHUNK, total - len(data))
        got_total, got_offset, chunk = _read_chunk(prog.link, want, len(data))
        if got_total != total or got_offset != len(data) or len(chunk) != want:
            raise CommsError("reading the stored script back gave an inconsistent answer; not trusting it.")
        data += chunk
    return bytes(data)


# ---------------------------------------------------------------------- backups
def backup_folder(backup_dir, uuid: str) -> Path:
    return Path(backup_dir) / uuid / "code"


def save_backup(prog, content: bytes, reason: str) -> Path:
    """Write atomically and read back; any failure raises, so the caller stops before erasing anything."""
    ident = prog.identify()
    folder = backup_folder(prog.backup_dir, ident["uuid"])
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**9:09d}"
    path = folder / f"{stamp}-{reason}.json"
    doc = {"app": "SKY programmer", "kind": "lisp-code", "format": FORMAT, "reason": reason,
           "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "hw": ident["hw"], "fw": ident["fw"],
           "uuid": ident["uuid"], "empty": not content, "imports": import_count(content) if content else 0,
           "b64": base64.b64encode(content).decode(), "sha256": hashlib.sha256(content).hexdigest()}  # fmt: skip
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if load_backup(path)["content"] != content:
            raise ValueError("it didn't read back the same")
    except (OSError, ValueError, KeyError, SafetyRefusal) as e:
        raise SafetyRefusal(f"could not save a script backup in {folder}: {e}. "
                            "Nothing was changed on the board.") from e
    return path


def load_backup(path) -> dict:
    try:
        with open(path) as f:
            doc = json.load(f)
        if not isinstance(doc, dict) or doc.get("kind") != "lisp-code" or doc.get("format") != FORMAT:
            raise ValueError("not a SKY programmer script backup")
        content = base64.b64decode(doc["b64"])
        if hashlib.sha256(content).hexdigest() != doc["sha256"]:
            raise ValueError("its checksum doesn't match, so it's damaged")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SafetyRefusal(f"{path} can't be used: {e}.") from e
    doc["content"], doc["path"] = content, str(path)
    return doc


def list_backups(backup_dir, uuid: str) -> list[Path]:
    folder = backup_folder(backup_dir, uuid)
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


# ---------------------------------------------------------------------- storing
def store(prog, content: bytes, reason: str = "pre-store") -> dict:
    """Back up, erase, write each chunk once, read every byte back. Leaves the new script stopped.

    Nothing is retried after the erase: a failure leaves no valid script, which the firmware treats as none, and the
    backup restores the old one.
    """
    new_image = image(content)
    if len(new_image) > CODE_AREA_BYTES:
        raise SafetyRefusal("the script doesn't fit the code area.")
    link = prog.link
    conf = prog.read_conf(MC_ROOT)
    prog.require_idle(conf)  # first: every erase and write releases the motor for 5 s
    old = read_code(prog)
    code_backup = save_backup(prog, old, reason)
    config_backup = prog.save_backup("pre-code")  # the new script may change config in RAM when it starts
    prog.require_idle(conf)

    prog.script_paused = True  # the erase stops the running script, even if the erase then fails
    try:
        reply = link.request(bytes([p.COMM_LISP_ERASE_CODE]) + struct.pack(">i", len(new_image) + 100),
                             timeout=ERASE_TIMEOUT_S, code_write=True)  # fmt: skip
    except CommsError as e:
        raise CommsError(f"no reply to the erase, so the script area's state is unknown and the script is stopped. "
                         f"Check with `sky code backup`. Old script: {code_backup}") from e  # fmt: skip
    if len(reply) < 2 or reply[1] != 1:
        raise VerifyFailed(f"the board didn't erase the script area. The script is stopped; nothing was written. "
                           f"Restore with `sky code restore {code_backup}`.")  # fmt: skip

    for offset in range(0, len(new_image), WRITE_CHUNK):
        chunk = new_image[offset:offset + WRITE_CHUNK]
        try:
            reply = link.request(bytes([p.COMM_LISP_WRITE_CODE]) + struct.pack(">I", offset) + chunk,
                                 timeout=WRITE_TIMEOUT_S, code_write=True)  # fmt: skip
        except CommsError as e:
            raise VerifyFailed(f"no reply to the write at byte {offset}. Not retried. The board now holds no valid "
                               f"script and boots without one; restore with `sky code restore {code_backup}`.") from e
        ok = len(reply) >= 6 and reply[1] == 1 and struct.unpack_from(">I", reply, 2)[0] == offset
        if not ok:
            raise VerifyFailed(f"the write at byte {offset} failed. The board now holds no valid script and boots "
                               f"without one; restore with `sky code restore {code_backup}`.")  # fmt: skip

    try:
        back = read_code(prog)
    except CommsError as e:
        raise VerifyFailed(f"the script was written but couldn't be read back, so it isn't proven. Check with "
                           f"`sky code backup`. Old script: {code_backup}") from e  # fmt: skip
    if back != content:
        raise VerifyFailed(f"the stored script doesn't read back as written. Restore with "
                           f"`sky code restore {code_backup}`.")  # fmt: skip
    return {"code_backup": str(code_backup), "config_backup": str(config_backup), "bytes": len(content)}


# ---------------------------------------------------------------------- trying from RAM
def try_script(prog, text: str) -> int:
    """Stream the script to RAM and run it alone. Nothing is stored; a power-up runs the stored script again.

    Each chunk is sent once. Above all the first: at offset 0 the firmware restarts LispBM, so a retry would too.
    """
    data = text.encode("utf-8")
    if not data:
        raise SafetyRefusal("the script is empty.")
    conf = prog.read_conf(MC_ROOT)
    prog.require_idle(conf)
    for offset in range(0, len(data), STREAM_CHUNK):
        chunk = data[offset:offset + STREAM_CHUNK]
        packet = (bytes([p.COMM_LISP_STREAM_CODE]) + struct.pack(">iib", offset, len(data), STREAM_MODE_RESET_AND_LOAD)
                  + chunk)  # fmt: skip
        try:
            reply = prog.link.request(packet, timeout=STREAM_TIMEOUT_S, code_stream=True)
        except CommsError as e:
            raise CommsError(f"no reply while sending byte {offset} of the script, so it may be partly running. "
                             "Power-cycle the board with the engine stopped to bring back the stored script.") from e
        result = struct.unpack_from(">h", reply, 5)[0] if len(reply) >= 7 else None
        if result != 0:
            raise VerifyFailed(f"the board refused the script at byte {offset} (result {result}). Power-cycle the "
                               "board with the engine stopped to bring back the stored script.")  # fmt: skip
    prog.script_paused = False  # the streamed script is running
    return len(data)
