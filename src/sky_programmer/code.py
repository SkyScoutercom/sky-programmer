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
    "cmds-start-stop": "it takes over the board's command handling",
    # Allowed when storing (native=True), never when trying: streaming ignores imports (docs/LISP_UPLOAD_SPEC.md).
    "import": "a script with imports can't run from RAM, because the firmware ignores imports when streaming. Store "
    "it instead", "load-native-lib": "native libraries can't run from RAM. Store the script instead",
    "unload-native-lib": "native libraries can't run from RAM. Store the script instead",
    # conf-set takes any value, unchecked, and these keep it: a bad switching frequency can hang the board at boot,
    # before USB starts (SAFETY.md). Change stored config with `sky set`, which checks it.
    "conf-store": "it stores config the programmer can't check; a bad value can hang the board at every boot",
    "conf-restore-mc": "it stores config the programmer can't check", "conf-restore-app": "it stores config the "
    "programmer can't check", "store-backup": "it stores config the programmer can't check",
    "can-send-sid": "it can send commands to other boards on CAN, including firmware commands",
    "can-send-eid": "it can send commands to other boards on CAN, including firmware commands",
    "can-cmd": "it runs code on other boards on CAN",
    # These build a word from data at run time, which would get past every check here (lispBM eval_cps.c: `apply`
    # on a symbol calls it).
    "str2sym": "it builds a word at run time, which gets past these checks",
    "u2sym": "it builds a word at run time, which gets past these checks",
    "unflatten": "it can build a word at run time, which gets past these checks",
    "read": "it turns text into code at run time, which gets past these checks",
    "read-program": "it turns text into code at run time, which gets past these checks",
    "read-eval": "it turns text into code at run time, which gets past these checks",
    "read-eval-program": "it turns text into code at run time, which gets past these checks",
}  # fmt: skip
# Words allowed but worth a warning.
WARNING_WORDS = {
    "eeprom-store-f": "it writes the board's EEPROM", "eeprom-store-i": "it writes the board's EEPROM",
}  # fmt: skip
NATIVE_WORDS = frozenset({"import", "load-native-lib", "unload-native-lib"})
HW_TYPE_VESC = 0  # the code area layout here is only known for STM32 VESC boards

# The start-up guard added to every stored script that uses native libraries. A library that crashes when it loads
# resets the board, which runs the script again: a boot loop. The firmware binds imports before the script runs but
# only runs a library when `load-native-lib` is evaluated (lispif.c, lispif_c_lib.c), and it answers USB commands, the
# erase among them, while the script sleeps (comm_usb.c). So waiting first leaves a window at every power-up in which
# `sky code rescue` can erase the script. USB comes up about 0.5 s before the script starts; 10 s covers the laptop
# re-detecting the board.
# It only waits while the motor is still. A reset with the engine running (a brown-out, EMI) must give the engine script
# back within about a second, as before: it regulates throttle and cuts overvoltage and overspeed. BLDC tracks the rpm
# of a released, spinning motor (mcpwm.c, back-EMF in MC_STATE_OFF), and the rescue refuses a turning motor anyway.
GUARD_SECONDS = 10
GUARD_IDLE_ERPM = 200  # programmer.IDLE_MAX_ERPM: a hand-turned engine reads 12 at most, a running one far more
GUARD_MARK = ";; SKY programmer start-up guard"
GUARD = (f"{GUARD_MARK}, added automatically: with the motor still, wait {GUARD_SECONDS} s after power-up\n"
         ";; before any native library loads, so `sky code rescue` can erase this script if a library keeps crashing\n"
         ";; the board. With the motor turning, as after a reset in flight, it doesn't wait.\n"
         f"(if (< (abs (get-rpm)) {GUARD_IDLE_ERPM}) (sleep {GUARD_SECONDS}))\n")  # fmt: skip
STREAM_DONE = "Stream done, starting..."  # lispif.c prints this only when the whole script arrived and starts


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


def check_script(text: str, *, native: bool = False) -> list[str]:
    """Refuse a script that would be unsafe to store or try. Returns warnings for words that are allowed.

    native: allow imports and native libraries, which only storing supports.
    """
    if "\0" in text:
        raise SafetyRefusal("the script contains a NUL character, which would end it early on the board.")
    found = set(symbols(text))
    if not native and find_imports(text):
        found.add("import")  # an import line VESC Tool would pack, even if the symbol reader saw it differently
    refused = sorted(found & (set(FORBIDDEN_WORDS) - (NATIVE_WORDS if native else set())))
    if refused:
        raise SafetyRefusal("this script can't be uploaded: " + "; ".join(f"`{w}`: {FORBIDDEN_WORDS[w]}"
                                                                          for w in refused) + ".")  # fmt: skip
    if native:
        _check_native_use(text)
    return [f"`{w}`: {WARNING_WORDS[w]}." for w in sorted(found & set(WARNING_WORDS))]


def _check_native_use(text: str) -> None:
    """A library may only be one of the script's imported files, named directly. `load-native-lib` runs any array,
    one built in RAM too (lispif_c_lib.c), so `(load-native-lib (bufcreate ...))` would run code nobody stored."""
    if text.startswith(GUARD_MARK) and not text.startswith(GUARD):
        raise SafetyRefusal("the start-up guard at the top of this script was changed. Delete those lines; storing "
                            "adds the guard again.")  # fmt: skip
    names = set()
    for number, _, name in find_imports(text):
        if not name.isascii():  # VESC Tool sizes its table by characters, so it would pack these at wrong offsets
            raise SafetyRefusal(f"line {number}: import names must be plain ASCII, not `{name}`.")
        names.add(name.lower())
    words = symbols(text)
    for i, word in enumerate(words):
        if word in ("load-native-lib", "unload-native-lib"):
            arg = words[i + 1] if i + 1 < len(words) else ""
            if arg not in names:
                raise SafetyRefusal(f"`{word}` must name one of this script's imports directly, as in "
                                    f"`({word} name)`; it's given `{arg or 'nothing'}`.")  # fmt: skip


def pack(text: str) -> bytes:
    """The stored content, as VESC Tool builds it with no imports: text, NUL, int16 import count 0."""
    content = text.encode("utf-8") + b"\0" + struct.pack(">h", 0)
    if len(content) > MAX_CONTENT_BYTES:
        raise SafetyRefusal(f"the script is {len(content)} bytes; the board holds at most {MAX_CONTENT_BYTES}.")
    return content


def find_imports(text: str) -> list[tuple[int, str, str]]:
    """(line number, path, name) of each import, read line by line exactly as VESC Tool's uploader reads them
    (codeloader.cpp, getImportFromLine), so a script packs the same in both tools."""
    found = []
    for number, line in enumerate(text.split("\n"), 1):
        line = line.lstrip(" ")
        while line.startswith("( "):
            line = "(" + line[2:]
        if not line.lower().startswith("(import "):
            continue
        start, end = line.find('"'), line.rfind('"')
        path = line[start + 1:end] if 0 < start < end else ""
        name = "".join(c for c in line[end + 1:] if c not in "\r )'") if 0 < start < end else ""
        name = name.split(";")[0]
        if not path or not name:
            raise SafetyRefusal(f"line {number}: this import can't be read: {line.strip()[:80]}")
        found.append((number, path, name))
    return found


def uses_native(text: str) -> bool:
    return bool(find_imports(text)) or bool(set(symbols(text)) & NATIVE_WORDS)


def has_guard(text: str) -> bool:
    """Exactly this tool's guard, unchanged: a marker with an edited guard below it guards nothing."""
    return text.startswith(GUARD)


def pack_script(text: str, folder, *, guard: bool = True, built: dict | None = None
                ) -> tuple[bytes, list[tuple[str, int]]]:  # fmt: skip
    """The stored content of a script and its imports, as VESC Tool's lispPackImports builds it, and (name, size) of
    each imported file. A script using native libraries gets the start-up guard first.

    built: import path -> bytes freshly built from source, used instead of the file on disk.

    Layout: text, NUL, int16 count, then per import its name, NUL, int32 offset, int32 size, then each file plus a NUL,
    4-byte aligned from the start of the content, because a library runs from where it's stored.
    """
    imports = find_imports(text)
    if not uses_native(text):
        return pack(text), []
    text = text if has_guard(text) or not guard else GUARD + text  # guard=False only to compare with VESC Tool
    files = []
    for number, path, name in imports:
        if path.startswith(("pkg::", "pkg@")):
            raise SafetyRefusal(f"line {number}: imports from VESC packages aren't supported. Use VESC Tool for this "
                                "script.")  # fmt: skip
        if built and path in built:
            files.append((name, built[path] + b"\0"))
            continue
        found = next((f for f in (Path(folder) / path, Path(path)) if f.is_file()), None)
        if found is None:
            raise SafetyRefusal(f"line {number}: imported file not found: {Path(folder) / path}")
        try:
            files.append((name, found.read_bytes() + b"\0"))  # VESC Tool adds the NUL in case it's a text file
        except OSError as e:
            raise SafetyRefusal(f"line {number}: can't read {found}: {e}") from e
    out = bytearray(text.encode("utf-8") + b"\0" + struct.pack(">h", len(files)))
    offset = len(out) + sum(len(name.encode("utf-8")) + 9 for name, _ in files)
    for name, data in files:
        offset += -offset % 4
        out += name.encode("utf-8") + b"\0" + struct.pack(">ii", offset, len(data))
        offset += len(data)
    for _, data in files:
        out += b"\0" * (-len(out) % 4)
        out += data
    if len(out) > MAX_CONTENT_BYTES:
        raise SafetyRefusal(f"the script and its imports are {len(out)} bytes; the board holds at most "
                            f"{MAX_CONTENT_BYTES}.")  # fmt: skip
    return bytes(out), [(name, len(data) - 1) for name, data in files]


def unpack_imports(content: bytes) -> list[tuple[str, bytes]]:
    """(name, data) of each file packed after the script text, data without VESC Tool's trailing NUL."""
    end = content.find(b"\0")
    count = import_count(content)
    out, i = [], end + 3
    for _ in range(max(count, 0)):
        stop = content.index(b"\0", i)
        name = content[i:stop].decode("utf-8", errors="replace")
        offset, size = struct.unpack_from(">ii", content, stop + 1)
        out.append((name, content[offset:offset + size].removesuffix(b"\0")))
        i = stop + 9
    return out


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


def require_vesc_hardware(prog) -> None:
    """The code area's size and place are those of an STM32 VESC; anything else could put a write somewhere else."""
    hw_type = prog.identify().get("hw_type")
    if hw_type != HW_TYPE_VESC:
        raise SafetyRefusal(f"this board reports hardware type {hw_type}, not an STM32 VESC (0). Script upload only "
                            "knows the STM32 VESC's script area, so it's refused.")  # fmt: skip


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


NATIVE_RISK = ("A native library is machine code with full access to the board: none of this tool's checks apply to "
               "it, and it can do anything the firmware can, erasing the firmware or bootloader included. Store only "
               "libraries you built or trust. If one crashes when it loads, the board resets and loads it again, "
               "over and over. Without the start-up guard, only the HALL2-SWCLK jumper, which also resets the config "
               "to defaults, or an SWD programmer recovers it (see SAFETY.md).")  # fmt: skip
GUARD_NOTE = (f"Scripts stored with the SKY programmer get a start-up guard: at a power-up with the motor still, they "
              f"wait {GUARD_SECONDS} s before loading a library, and `sky code rescue` can erase the script in that "
              "window. After a reset with the motor turning, they don't wait.")  # fmt: skip


def library_lines(files) -> list[str]:
    """One line per imported file, with its SHA-256, so the operator can tell exactly which build is stored."""
    return [f"{name}: {len(data)} bytes, SHA-256 {hashlib.sha256(data).hexdigest()}" for name, data in files]


def check_restore(doc: dict, ident: dict) -> int:
    """Refuse a backup from another board or an empty one. Returns how many imported files it holds."""
    if (doc["uuid"], doc["hw"], doc["fw"]) != (ident["uuid"], ident["hw"], ident["fw"]):
        raise SafetyRefusal(f"this backup is from {doc['hw']} firmware {doc['fw']}, UUID {doc['uuid']}; the board is "
                            f"{ident['hw']} firmware {ident['fw']}, UUID {ident['uuid']}.")  # fmt: skip
    if doc["empty"] or not doc["content"]:
        raise SafetyRefusal("this backup holds no script, so there is nothing to restore.")
    return import_count(doc["content"])


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
    require_vesc_hardware(prog)
    link = prog.link
    try:
        conf = prog.read_conf(MC_ROOT)
        prog.require_idle(conf)  # first: every erase and write releases the motor for 5 s
        old = read_code(prog)
        code_backup = save_backup(prog, old, reason)
        config_backup = prog.save_backup("pre-code")  # the new script may change config in RAM when it starts
        prog.require_idle(conf)
    except CommsError as e:  # the bench showed a cable pulled here otherwise reads like a failed store
        raise CommsError(f"{str(e).rstrip('.')}. This was before the erase, so nothing was written and the stored "
                         "script is unchanged.") from e  # fmt: skip

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
        # The firmware skips the erase when the area's first byte is already blank (flash_helper.c only tests byte 0),
        # so old data further in, such as a large firmware update's tail, can spoil the first try. That byte is
        # written now, so the next erase runs.
        raise VerifyFailed(f"the stored script doesn't read back as written, so the board holds no valid script. Run "
                           f"the same command once more: the firmware can skip the first erase when old data is "
                           f"left in the area. If it fails again, restore with "
                           f"`sky code restore {code_backup}`.")  # fmt: skip
    return {"code_backup": str(code_backup), "config_backup": str(config_backup), "bytes": len(content)}


# ---------------------------------------------------------------------- trying from RAM
def _saw_stream_done(link, wait: float = 1.5) -> bool:
    """Whether the board printed that the stream finished and the script starts. Other prints are kept."""
    seen = link.take_prints()
    t0 = link.clock()
    while not any(STREAM_DONE in s for s in seen) and link.clock() - t0 < wait:
        seen += link.collect_prints(0.05)
    done = any(STREAM_DONE in s for s in seen)
    link.prints.extend(s for s in seen if STREAM_DONE not in s)
    return done


def try_script(prog, text: str) -> dict:
    """Stream the script to RAM and run it alone. The stored script is unchanged; a power-up runs it again.

    Each chunk is sent once. Above all the first: at offset 0 the firmware restarts LispBM, so a retry would too.
    Success is only reported once the board says it started the script: after an earlier stream that broke off, the
    firmware can answer a new first chunk with that stream's stale result and run nothing (lispif.c, offset_last).
    """
    data = text.encode("utf-8")
    if not data:
        raise SafetyRefusal("the script is empty.")
    require_vesc_hardware(prog)
    conf = prog.read_conf(MC_ROOT)
    prog.require_idle(conf)
    config_backup = prog.save_backup("pre-try")  # the script may change config in RAM; this records it before
    prog.require_idle(conf)
    prog.link.take_prints()  # the stream-done check below must see only this stream's messages
    prog.script_paused = True  # the first chunk stops the stored script
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
    if not _saw_stream_done(prog.link):
        raise VerifyFailed("the board took the script but didn't confirm it started, so it may be running nothing. "
                           "Power-cycle the board with the engine stopped, then try again.")  # fmt: skip
    prog.script_paused = False  # the streamed script is running
    return {"bytes": len(data), "config_backup": str(config_backup)}


# ---------------------------------------------------------------------- breaking a boot loop
RESCUE_WAIT_S = 120.0
# The script starts about 0.5 s after USB (commands.c waits for main to finish), and an erase during its start-up
# could meet an uninitialised mutex (lispif.c), so wait this long after the board answers. The guard leaves ~9 s more.
RESCUE_SETTLE_S = 2.0


def rescue(open_link, find_ports, backup_dir, *, uuid: str | None = None, wait_s: float = RESCUE_WAIT_S,
           clock=time.monotonic, sleep=time.sleep, say=print, should_stop=lambda: False) -> dict:  # fmt: skip
    """Wait for the board to appear, then erase its stored script, so its next boot runs none.

    For a board boot-looping on a native library: the erase stops the script first (commands.c, lispif_stop), and the
    start-up guard keeps the library from loading for GUARD_SECONDS. A board that resets midway is waited for again.
    should_stop: checked while waiting, so the desktop app can cancel before anything is erased.
    """
    from .programmer import Programmer  # here, so importing this module stays light

    deadline, last_problem = clock() + wait_s, "the board never appeared"
    while clock() < deadline:
        if should_stop():
            raise SafetyRefusal("the rescue was cancelled. Nothing was erased.")
        ports = find_ports()
        if len(ports) > 1:
            raise SafetyRefusal(f"more than one possible board ({', '.join(ports)}). Pick one with --port.")
        if not ports:
            sleep(0.05)
            continue
        try:
            link = open_link(ports[0])
        except CommsError as e:  # e.g. ModemManager still probing the new port
            last_problem = str(e)
            sleep(0.1)
            continue
        try:
            prog = Programmer(link, backup_dir, say=lambda *_: None)
            ident = prog.identify()
            if uuid and ident["uuid"] != uuid.lower():
                raise SafetyRefusal(f"the board on {ports[0]} is UUID {ident['uuid']}, not {uuid}. Nothing was erased.")
            require_vesc_hardware(prog)
            prog.require_idle(prog.read_conf(MC_ROOT))
            say(f"Found {ident['hw']}, UUID {ident['uuid']}, on {ports[0]}. Erasing its script in "
                f"{RESCUE_SETTLE_S:g} s.")  # fmt: skip
            t0 = clock()
            try:  # the settle time is spent saving what's about to be erased, if the disk allows
                say(f"Backed up its script first: {save_backup(prog, read_code(prog), 'pre-rescue')}")
            except SafetyRefusal as e:
                say(f"Couldn't back up its script ({e}). Erasing anyway.")
            sleep(max(0.0, RESCUE_SETTLE_S - (clock() - t0)))
            reply = link.request(bytes([p.COMM_LISP_ERASE_CODE]) + struct.pack(">i", CODE_AREA_BYTES),
                                 timeout=ERASE_TIMEOUT_S, code_write=True)  # fmt: skip
            if len(reply) < 2 or reply[1] != 1:
                raise VerifyFailed("the board didn't confirm the erase. Run the rescue again.")
            if read_code(prog):
                raise VerifyFailed("a script is still stored after the erase. Run the rescue again.")
            return {"port": ports[0], "uuid": ident["uuid"], "hw": ident["hw"]}
        except CommsError as e:  # it reset in the middle: wait for it to come back
            last_problem = str(e)
            say("Lost the board, probably a reset. Waiting for it to come back.")
        finally:
            link.close()
    raise CommsError(f"no rescue within {wait_s:g} s: {last_problem}. If the script has no start-up guard, use the "
                     "HALL2-SWCLK jumper or an SWD programmer (SAFETY.md).")  # fmt: skip
