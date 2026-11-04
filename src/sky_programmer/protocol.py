"""VESC packet framing and value coding. Pure functions: no I/O happens here.

Command numbers are from COMM_PACKET_ID in the firmware's datatypes.h (release_7_00).
"""
from __future__ import annotations

import math
import struct

COMM_FW_VERSION = 0
COMM_GET_VALUES = 4
COMM_SET_MCCONF = 13
COMM_GET_MCCONF = 14
COMM_GET_MCCONF_DEFAULT = 15
COMM_GET_APPCONF = 17
COMM_TERMINAL_CMD = 20
COMM_PRINT = 21
COMM_LISP_SET_RUNNING = 133
COMM_LISP_PRINT = 135
COMM_LISP_REPL_CMD = 138
COMM_MOTOR_ESTOP = 159

PRINT_COMMANDS = (COMM_PRINT, COMM_LISP_PRINT)


def crc16(data: bytes) -> int:
    """CRC-16/XMODEM (poly 0x1021, init 0), as used by the VESC packet format."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def frame(payload: bytes) -> bytes:
    n = len(payload)
    if n <= 255:
        head = bytes([2, n])
    elif n <= 65535:
        head = bytes([3, n >> 8, n & 0xFF])
    else:
        head = bytes([4, n >> 16, (n >> 8) & 0xFF, n & 0xFF])
    return head + payload + struct.pack(">H", crc16(payload)) + b"\x03"


def frame_at(buf: bytes, i: int):
    """(payload, end) if a complete, CRC-valid packet starts at buf[i], else None."""
    st = buf[i]
    if st not in (2, 3, 4):
        return None
    hl = st - 1
    if len(buf) < i + 1 + hl:
        return None
    n = int.from_bytes(buf[i + 1:i + 1 + hl], "big")
    end = i + 1 + hl + n + 3
    if len(buf) < end or buf[end - 1] != 3:
        return None
    pl = buf[i + 1 + hl:i + 1 + hl + n]
    if struct.unpack(">H", buf[end - 3:end - 1])[0] != crc16(pl):
        return None
    return pl, end


class Reader:
    """Big-endian reader for VESC buffers."""

    def __init__(self, b: bytes):
        self.b, self.i = b, 0

    def _take(self, fmt: str):
        v = struct.unpack_from(fmt, self.b, self.i)[0]
        self.i += struct.calcsize(fmt)
        return v

    def u8(self):
        return self._take(">B")

    def i8(self):
        return self._take(">b")

    def u16(self):
        return self._take(">H")

    def i16(self):
        return self._take(">h")

    def u32(self):
        return self._take(">I")

    def i32(self):
        return self._take(">i")


def dec_f32auto(r: int) -> float:
    """buffer_get_float32_auto() from the firmware's buffer.c."""
    e = (r >> 23) & 0xFF
    sig_i = r & 0x7FFFFF
    neg = bool(r & (1 << 31))
    sig = 0.0
    if e != 0 or sig_i != 0:
        sig = sig_i / (8388608.0 * 2.0) + 0.5
        e -= 126
    if neg:
        sig = -sig
    return sig * (2.0**e)


def enc_f32auto(x: float) -> bytes:
    """buffer_append_float32_auto() from the firmware's buffer.c; returns 4 bytes."""
    x = struct.unpack(">f", struct.pack(">f", x))[0]
    if abs(x) < 1.5e-38:
        x = 0.0
    sig, e = math.frexp(x)
    sig_abs = abs(sig)
    sig_i = 0
    if sig_abs >= 0.5:
        sig_i = int((sig_abs - 0.5) * 2.0 * 8388608.0)
        e += 126
    res = ((e & 0xFF) << 23) | (sig_i & 0x7FFFFF)
    if sig < 0:
        res |= 1 << 31
    return struct.pack(">I", res)


def parse_fw_version(pl: bytes) -> dict:
    """COMM_FW_VERSION reply payload -> dict."""
    major, minor = pl[1], pl[2]
    rest = pl[3:]
    name = rest.split(b"\0", 1)[0].decode(errors="replace")
    after = rest[len(name) + 1:]
    return {
        "fw": f"{major}.{minor:02d}",
        "major": major,
        "minor": minor,
        "hw": name,
        "uuid": after[:12].hex(),
        "test_version": after[13] if len(after) > 13 else None,
    }


def parse_values(pl: bytes) -> dict:
    """COMM_GET_VALUES reply payload -> dict of the fields up to the fault code."""
    v = Reader(pl[1:])
    return {
        "temp_fet": v.i16() / 10,
        "temp_motor": v.i16() / 10,
        "current_motor": v.i32() / 100,
        "current_in": v.i32() / 100,
        "id": v.i32() / 100,
        "iq": v.i32() / 100,
        "duty": v.i16() / 1000,
        "erpm": v.i32(),
        "v_in": v.i16() / 10,
        "amp_hours": v.i32() / 1e4,
        "amp_hours_charged": v.i32() / 1e4,
        "watt_hours": v.i32() / 1e4,
        "watt_hours_charged": v.i32() / 1e4,
        "tachometer": v.i32(),
        "tachometer_abs": v.i32(),
        "fault_code": v.u8(),
    }
