"""A simulated VESC that speaks the real packet format, with failures on demand.

It answers from a real 7.00-layout config, records every command, and notes any forbidden command
in `violations`. Time is simulated, so timeout tests finish instantly.

These firmware behaviours are modelled on purpose, because each one once let a test pass for the wrong reason:
- SET_MCCONF stores values as float32 and truncates scaled fields, so some read back one step lower;
- a REPL expression resumes a paused script;
- SET_RUNNING 0 answers "yes" whenever a script exists, "no" at once if none does, "no" after 2 s if the pause fails;
- SET_RUNNING 1 answers "no" with no stored script;
- only real terminal commands answer, and only allowed meta commands print;
- a bad BLDC switching frequency hangs the next boot, so USB never comes back;
- a pause that times out stays pending, so the script stops later even though the answer was "no";
- a REPL expression that can't pause first prints "Could not pause" and doesn't resume the script;
- a REPL line within 0.5 s of the last is dropped with no reply;
- only a meta command at the very start of the line counts as one: anything else is an expression.
"""
from __future__ import annotations

import math
import struct

from . import code, config, safety
from . import protocol as p
from .config import APP_ROOT, MC_ROOT

UUID = bytes(range(1, 13))
STORED_SCRIPT = '(print "stored script running")'
LAYOUT = config.load_layout(7, 0)

BOARD_VALUES = {
    "motor_type": 0, "si_motor_poles": 28, "l_current_max": 70.0, "l_current_min": -60.0,
    "l_in_current_max": 60.0, "l_in_current_min": -20.0, "l_abs_current_max": 140.0, "l_min_vin": 8.0,
    "l_max_vin": 70.0, "l_battery_cut_start": 40.8, "l_battery_cut_end": 36.0, "l_max_duty": 0.95,
    "l_min_duty": 0.005, "m_bldc_f_sw_min": 30000.0, "m_bldc_f_sw_max": 35000.0, "m_dc_f_sw": 25000.0,
    "l_temp_fet_start": 85.0,
    "l_temp_fet_end": 100.0, "l_temp_motor_start": 85.0, "l_temp_motor_end": 100.0, "foc_motor_r": 0.0081,
    "foc_motor_l": 4.97e-6, "foc_motor_flux_linkage": 0.0018, "foc_current_kp": 0.03, "foc_current_ki": 10.0,
    "foc_f_zv": 25000.0, "sl_bemf_coupling_k": 150.0, "foc_offsets_current[0]": 2048.0,
}  # fmt: skip
DEFAULT_VALUES = {**BOARD_VALUES, "foc_motor_r": 0.015, "foc_motor_l": 7.5e-6, "foc_motor_flux_linkage": 0.00245}


def build_blob(values: dict, root: str = MC_ROOT) -> bytes:
    out = bytearray()
    for kind, field, scale in LAYOUT.ops[root]:
        if kind == "sig":
            out += struct.pack(">I", LAYOUT.signature(root))
        else:
            out += config.encode_value(kind, scale, values.get(field, 0))
    return bytes(out)


def _f32(x):
    return struct.unpack(">f", struct.pack(">f", x))[0]


def _as_firmware_stores_it(blob: bytes) -> bytes:
    """Scaled fields become float32 on deserialize and are truncated, not rounded, on serialize."""
    out, off = bytearray(blob), 0
    for kind, _field, scale in LAYOUT.ops[MC_ROOT]:
        n = config.SIZES[kind]
        if kind == "f16":
            raw = struct.unpack_from(">h", blob, off)[0]
            stored = _f32(_f32(raw / _f32(scale)) * _f32(scale))
            struct.pack_into(">h", out, off, math.trunc(stored))
        off += n
    return bytes(out)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeVesc:
    """Stands in for serial.Serial."""

    def __init__(self, clock=None, fw=(7, 0), hw="Flipsky_110_100", uuid=UUID, values=None, **knobs):
        self.clock = clock or Clock()
        self.fw, self.hw, self.uuid = fw, hw, uuid
        self.mc = build_blob({**BOARD_VALUES, **(values or {})})
        self.default = build_blob(DEFAULT_VALUES)
        self.app = build_blob({}, APP_ROOT)
        self.rx, self.tx = b"", b""
        self.commands, self.written, self.violations, self.prints_on_term = [], [], [], ["fault log empty"]
        self.erpm, self.current, self.duty, self.v_in, self.fault = 0, 0.0, 0.0, 48.0, 0
        self.temp_fet, self.temp_motor = 25.0, 24.0
        self.script_present = knobs.pop("script_present", True)
        self.script_running = self.script_present
        self.pause_fails = knobs.pop("pause_fails", False)
        self.lose_running_reply = knobs.pop("lose_running_reply", False)  # SET_RUNNING acts, but the reply is lost
        self.repl_cannot_pause = knobs.pop("repl_cannot_pause", False)
        self.repl_ignored = False
        self.linked_limit = knobs.pop("linked_limit", False)  # firmware's foc_sl_erpm_start <= 0.9 * foc_sl_erpm
        self.boot_hung = False
        self.pause_pending = False
        self.write_mode = knobs.pop("write_mode", "ok")  # ok, drop_ack, ignored, dead, reject
        self.clamp = knobs.pop("clamp", {})  # field -> value the firmware stores instead
        self.side_effect = knobs.pop("side_effect", {})  # field -> value it wrongly changes
        self.side_effect_writes = knobs.pop("side_effect_writes", 1)  # how many writes get it
        self.spin_after_pause = knobs.pop("spin_after_pause", 0)
        self.change_mc_on_read = knobs.pop("change_mc_on_read", False)
        self.dead = False
        self.released = False
        # The Lisp code area, sector 10. It starts with a small valid script when one is "present".
        self.code_area = bytearray(b"\xff" * code.CODE_AREA_BYTES)
        if self.script_present:
            stored = code.image(code.pack(STORED_SCRIPT))
            self.code_area[:len(stored)] = stored
        self.code_touched = False  # erased or written since start: then the code area decides whether a script runs
        self.code_writes, self.code_erases, self.stream_starts = [], 0, 0
        self.streamed = bytearray()
        self.code_erase_fails = knobs.pop("code_erase_fails", False)
        self.code_write_fails_at = knobs.pop("code_write_fails_at", None)  # offset whose write reports failure
        self.code_write_silent_at = knobs.pop("code_write_silent_at", None)  # offset whose write gets no reply
        self.code_read_corrupt = knobs.pop("code_read_corrupt", False)  # read-back returns one wrong byte
        self.stream_fails_at = knobs.pop("stream_fails_at", None)  # offset whose stream chunk is refused
        assert not knobs, knobs

    # ---------------------------------------------------------- serial interface
    def write(self, data):
        self.rx += data
        i = 0
        while i < len(self.rx):
            found = p.frame_at(self.rx, i)
            if found is None:
                i += 1
                continue
            pl, end = found
            self.rx, i = self.rx[end:], 0
            self._handle(pl)
        return len(data)

    def read(self, n):
        if not self.tx:
            self.clock.t += 0.05
            return b""
        out, self.tx = self.tx[:n], self.tx[n:]
        return out

    def close(self):
        pass

    # ---------------------------------------------------------- firmware behaviour
    def _reply(self, pl):
        if not self.dead:
            self.tx += p.frame(pl)

    def _print(self, cmd, text):
        self._reply(bytes([cmd]) + text.encode())

    def _handle(self, pl):
        cmd = pl[0]
        self.commands.append(cmd)
        if cmd in safety.FORBIDDEN or cmd not in safety.ALLOWED:
            self.violations.append(cmd)
        if self.dead:
            return
        if cmd == p.COMM_FW_VERSION:
            body = bytes([cmd, *self.fw]) + self.hw.encode() + b"\0" + self.uuid + bytes([0, 0])
            self._reply(body)
        elif cmd == p.COMM_GET_VALUES:
            self._reply(self._values())
        elif cmd == p.COMM_GET_MCCONF:
            if self.change_mc_on_read:
                values = config.decode(self.mc, LAYOUT, MC_ROOT)
                values["l_max_erpm"] = values["l_max_erpm"] + 100
                self.mc = config.patch_blob(self.mc, LAYOUT, MC_ROOT, {"l_max_erpm": values["l_max_erpm"]})
            self._reply(bytes([cmd]) + self.mc)
        elif cmd == p.COMM_GET_MCCONF_DEFAULT:
            self._reply(bytes([cmd]) + self.default)
        elif cmd == p.COMM_GET_APPCONF:
            self._reply(bytes([cmd]) + self.app)
        elif cmd == p.COMM_LISP_SET_RUNNING:
            self._set_running(bool(pl[1]))
        elif cmd == p.COMM_SET_MCCONF:
            self._write_config(pl[1:])
        elif cmd == p.COMM_TERMINAL_CMD:
            if pl[1:].rstrip(b"\0").decode() in safety.TERMINAL_READ_ONLY:
                for text in self.prints_on_term:
                    self._print(p.COMM_PRINT, text)
        elif cmd == p.COMM_LISP_REPL_CMD:
            text = pl[1:].rstrip(b"\0").decode()
            if self.repl_ignored:  # within 0.5 s of the last line, the firmware drops it with no reply
                return
            if self.repl_cannot_pause and not text.startswith(":"):
                self._print(p.COMM_LISP_PRINT, "Could not pause")
                return
            if not text.startswith(":"):
                self.script_running = self.script_present  # firmware: evaluation always continues the evaluator
            self._print(p.COMM_LISP_PRINT, "> " + text)
        elif cmd == p.COMM_MOTOR_ESTOP:
            self.released = True
        elif cmd == p.COMM_LISP_READ_CODE:
            self._read_code(pl)
        elif cmd == p.COMM_LISP_ERASE_CODE:
            self._erase_code()
        elif cmd == p.COMM_LISP_WRITE_CODE:
            self._write_code(pl)
        elif cmd == p.COMM_LISP_STREAM_CODE:
            self._stream_code(pl)

    # ---------------------------------------------------------- the Lisp code area (docs/LISP_UPLOAD_SPEC.md)
    def stored_content(self):
        """What the firmware would run: the content if the length and CRC check out (qmlui_check), else None."""
        area = bytes(self.code_area)
        if getattr(self, "_checked", (None,))[0] != area:  # the pure-Python CRC is slow; tests may edit the area
            n, crc = struct.unpack_from(">IH", area, 0)
            ok = n <= code.MAX_CONTENT_BYTES and p.crc16(area[6:8 + n]) == crc
            self._checked = (area, area[8:8 + n] if ok else None)
        return self._checked[1]

    def _read_code(self, pl):
        length, offset = struct.unpack_from(">ii", pl, 1)
        content = self.stored_content()
        if content is None:
            self._reply(bytes([p.COMM_LISP_READ_CODE]) + struct.pack(">ii", 0, 0))
            return
        if length + offset > len(content) or length > 502:
            return  # the firmware doesn't answer
        data = bytearray(content[offset:offset + length])
        if self.code_read_corrupt and data:
            data[0] ^= 0x01
        self._reply(bytes([p.COMM_LISP_READ_CODE]) + struct.pack(">ii", len(content), offset) + bytes(data))

    def _erase_code(self):
        self.code_erases += 1
        self.code_touched = True
        self.script_running = False  # lispif_stop runs first, even if the erase fails
        if self.erpm:
            self.violations.append("code erase while the motor was turning")
        if not self.code_erase_fails:
            self.code_area[:] = b"\xff" * code.CODE_AREA_BYTES
        self._reply(bytes([p.COMM_LISP_ERASE_CODE, 0 if self.code_erase_fails else 1]))

    def _write_code(self, pl):
        offset = struct.unpack_from(">I", pl, 1)[0]
        data = pl[5:]
        self.code_writes.append(offset)
        self.code_touched = True
        if not data or offset + len(data) > code.CODE_AREA_BYTES:
            self.violations.append(f"code write outside the code area: offset {offset}, {len(data)} bytes")
            return
        if offset == self.code_write_silent_at:
            return
        ok = offset != self.code_write_fails_at
        if ok:
            for i, byte in enumerate(data):  # flash programming can only clear bits
                self.code_area[offset + i] &= byte
        self._reply(bytes([p.COMM_LISP_WRITE_CODE, 1 if ok else 0]) + struct.pack(">I", offset))

    def _stream_code(self, pl):
        offset, total, mode = struct.unpack_from(">iib", pl, 1)
        if mode != code.STREAM_MODE_RESET_AND_LOAD:
            self.violations.append(f"stream mode {mode}")
        if offset == 0:
            self.stream_starts += 1  # the firmware restarts LispBM here, without the stored code
            self.streamed = bytearray()
            self.script_running = True
        result = -5 if offset == self.stream_fails_at else 0
        if result == 0:
            self.streamed += pl[10:]
        self._reply(bytes([p.COMM_LISP_STREAM_CODE]) + struct.pack(">ih", offset, result))

    def _set_running(self, run):
        if run and self.code_touched:  # a restart runs the stored code only if it checks out
            self.script_present = self.stored_content() is not None
        if run:
            self.script_running = self.script_present
            if not self.lose_running_reply:
                self._reply(bytes([p.COMM_LISP_SET_RUNNING, 1 if self.script_present else 0]))
            return
        if self.script_present and self.pause_fails:
            self.clock.sleep(2.0)
            self.pause_pending = True  # lbm_pause_eval isn't cancelled by the timeout
            self._reply(bytes([p.COMM_LISP_SET_RUNNING, 0]))
            return
        if self.spin_after_pause:
            self.erpm = self.spin_after_pause
        self.script_running = False
        if not self.lose_running_reply:
            self._reply(bytes([p.COMM_LISP_SET_RUNNING, 1 if self.script_present else 0]))

    def power_cycle(self):
        """Reboot from the stored config. A bad switching frequency in BLDC hangs in do_dc_cal, before USB."""
        values = config.decode(self.mc, LAYOUT, MC_ROOT)
        bad_f_sw = not (1000 <= values["m_bldc_f_sw_min"] and values["m_bldc_f_sw_max"] <= 200000)
        if values["motor_type"] == 0 and bad_f_sw:
            self.boot_hung = self.dead = True
        if self.code_touched:  # at boot the firmware runs the stored code only if it checks out
            self.script_present = self.stored_content() is not None
        self.script_running = self.script_present

    def _write_config(self, blob):
        self.written.append(blob)
        if self.script_running:
            self.violations.append("config write while the script was running")
        if self.erpm:
            self.violations.append("config write while the motor was turning")
        mode = self.write_mode
        if mode == "dead":
            self.dead = True
            return
        if mode in ("ignored", "reject"):
            return
        extra = dict(self.clamp)
        if self.side_effect and len(self.written) <= self.side_effect_writes:
            extra.update(self.side_effect)
        self.mc = _as_firmware_stores_it(config.patch_blob(blob, LAYOUT, MC_ROOT, extra))
        if self.linked_limit:
            values = config.decode(self.mc, LAYOUT, MC_ROOT)
            limit = values["foc_sl_erpm"] * 0.9
            if values["foc_sl_erpm_start"] > limit:
                self.mc = config.patch_blob(self.mc, LAYOUT, MC_ROOT, {"foc_sl_erpm_start": limit})
        if mode == "ok":
            self._reply(bytes([p.COMM_SET_MCCONF]))

    def _values(self):
        return struct.pack(">Bhhiiiihihiiiiii" + "B", p.COMM_GET_VALUES, int(self.temp_fet * 10),
                           int(self.temp_motor * 10), int(self.current * 100), 0, 0, 0,
                           int(self.duty * 1000), self.erpm, int(self.v_in * 10), 0, 0, 0, 0, 0, 0, self.fault)

    def value(self, field):
        return config.decode(self.mc, LAYOUT, MC_ROOT)[field]
