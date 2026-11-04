"""A simulated VESC that speaks the real packet format, with failures on demand.

It answers from a real 7.00-layout config, records every command, and notes any forbidden command
in `violations`. Time is simulated, so timeout tests finish instantly.

These firmware behaviours are modelled on purpose, because each one once let a test pass for the wrong reason:
- SET_MCCONF stores values as float32 and truncates scaled fields, so some read back one step lower;
- a REPL expression resumes a paused script;
- SET_RUNNING 0 answers "yes" whenever a script exists, "no" at once if none does, "no" after 2 s if the pause fails;
- SET_RUNNING 1 answers "no" with no stored script;
- only real terminal commands answer, and only allowed meta commands print;
- a bad BLDC switching frequency hangs the next boot, so USB never comes back.
"""
from __future__ import annotations

import math
import struct

from . import config, safety
from . import protocol as p
from .config import APP_ROOT, MC_ROOT

UUID = bytes(range(1, 13))
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
        self.linked_limit = knobs.pop("linked_limit", False)  # firmware's foc_sl_erpm_start <= 0.9 * foc_sl_erpm
        self.boot_hung = False
        self.write_mode = knobs.pop("write_mode", "ok")  # ok, drop_ack, ignored, dead, reject
        self.clamp = knobs.pop("clamp", {})  # field -> value the firmware stores instead
        self.side_effect = knobs.pop("side_effect", {})  # field -> value it wrongly changes
        self.side_effect_writes = knobs.pop("side_effect_writes", 1)  # how many writes get it
        self.spin_after_pause = knobs.pop("spin_after_pause", 0)
        self.change_mc_on_read = knobs.pop("change_mc_on_read", False)
        self.dead = False
        self.released = False
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
            if not text.startswith(":"):
                self.script_running = self.script_present  # firmware: evaluation always continues the evaluator
            self._print(p.COMM_LISP_PRINT, "> " + text)
        elif cmd == p.COMM_MOTOR_ESTOP:
            self.released = True

    def _set_running(self, run):
        if run:
            self.script_running = self.script_present
            self._reply(bytes([p.COMM_LISP_SET_RUNNING, 1 if self.script_present else 0]))
            return
        if self.script_present and self.pause_fails:
            self.clock.sleep(2.0)
            self._reply(bytes([p.COMM_LISP_SET_RUNNING, 0]))
            return
        if self.spin_after_pause:
            self.erpm = self.spin_after_pause
        self.script_running = False
        self._reply(bytes([p.COMM_LISP_SET_RUNNING, 1 if self.script_present else 0]))

    def power_cycle(self):
        """Reboot from the stored config. A bad switching frequency in BLDC hangs in do_dc_cal, before USB."""
        values = config.decode(self.mc, LAYOUT, MC_ROOT)
        bad_f_sw = not (1000 <= values["m_bldc_f_sw_min"] and values["m_bldc_f_sw_max"] <= 200000)
        if values["motor_type"] == 0 and bad_f_sw:
            self.boot_hung = self.dead = True
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
