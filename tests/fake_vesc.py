"""A simulated VESC that speaks the real packet format, with failures on demand.

It answers from a real 7.00-layout config, records every command, and notes any forbidden command
in `violations`. Time is simulated, so timeout tests finish instantly.
"""
from __future__ import annotations

import struct

from sky_programmer import config, safety
from sky_programmer import protocol as p
from sky_programmer.config import APP_ROOT, MC_ROOT

UUID = bytes(range(1, 13))
LAYOUT = config.load_layout(7, 0)

BOARD_VALUES = {
    "motor_type": 0, "si_motor_poles": 28, "l_current_max": 70.0, "l_current_min": -60.0,
    "l_in_current_max": 60.0, "l_in_current_min": -20.0, "l_abs_current_max": 140.0, "l_min_vin": 8.0,
    "l_max_vin": 70.0, "l_battery_cut_start": 40.8, "l_battery_cut_end": 36.0, "l_max_duty": 0.95,
    "l_min_duty": 0.005, "m_bldc_f_sw_min": 30000.0, "m_bldc_f_sw_max": 35000.0, "l_temp_fet_start": 85.0,
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


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeVesc:
    """Stands in for serial.Serial."""

    def __init__(self, clock=None, fw=(7, 0), hw="60_MK6", uuid=UUID, values=None, **knobs):
        self.clock = clock or Clock()
        self.fw, self.hw, self.uuid = fw, hw, uuid
        self.mc = build_blob({**BOARD_VALUES, **(values or {})})
        self.default = build_blob(DEFAULT_VALUES)
        self.app = build_blob({}, APP_ROOT)
        self.rx, self.tx = b"", b""
        self.commands, self.written, self.violations, self.prints_on_term = [], [], [], ["fault log empty"]
        self.erpm, self.current, self.duty, self.v_in, self.fault = 0, 0.0, 0.0, 48.0, 0
        self.script_running = True
        self.write_mode = knobs.pop("write_mode", "ok")  # ok, drop_ack, ignored, dead, reject
        self.clamp = knobs.pop("clamp", {})  # field -> value the firmware stores instead
        self.side_effect = knobs.pop("side_effect", {})  # field -> value it wrongly changes
        self.side_effect_writes = knobs.pop("side_effect_writes", 1)  # how many writes get it
        self.spin_after_pause = knobs.pop("spin_after_pause", 0)
        self.change_mc_on_read = knobs.pop("change_mc_on_read", False)
        self.dead = False
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
        elif cmd == p.COMM_LISP_GET_STATS:
            self._reply(struct.pack(">Bhhh", cmd, 123, 4500, 6700))
        elif cmd == p.COMM_LISP_SET_RUNNING:
            was_running = self.script_running
            self.script_running = bool(pl[1])
            if not pl[1] and self.spin_after_pause:
                self.erpm = self.spin_after_pause
            self._reply(bytes([cmd, 1 if (was_running or pl[1]) else 0]))
        elif cmd == p.COMM_SET_MCCONF:
            self._write_config(pl[1:])
        elif cmd == p.COMM_TERMINAL_CMD:
            for text in self.prints_on_term:
                self._print(p.COMM_PRINT, text)
        elif cmd == p.COMM_LISP_REPL_CMD:
            self._print(p.COMM_LISP_PRINT, "> " + pl[1:].rstrip(b"\0").decode())

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
        self.mc = config.patch_blob(blob, LAYOUT, MC_ROOT, extra)
        if mode == "ok":
            self._reply(bytes([p.COMM_SET_MCCONF]))

    def _values(self):
        return struct.pack(">Bhhiiiihihiiiiii" + "B", p.COMM_GET_VALUES, 250, 240, int(self.current * 100), 0, 0, 0,
                           int(self.duty * 1000), self.erpm, int(self.v_in * 10), 0, 0, 0, 0, 0, 0, self.fault)

    def value(self, field):
        return config.decode(self.mc, LAYOUT, MC_ROOT)[field]
