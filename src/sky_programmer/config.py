"""Decode, patch and validate VESC config blobs, using the layout of one exact firmware release."""
from __future__ import annotations

import json
import math
import struct
from importlib import resources

from . import protocol as p
from .errors import SafetyRefusal

MC_ROOT, APP_ROOT = "mc_configuration", "app_configuration"
SIZES = {"sig": 4, "u8": 1, "i8": 1, "f32auto": 4, "f16": 2, "f32": 4, "u16": 2, "i16": 2, "u32": 4, "i32": 4}
_INT = {"u8": (">B", 0, 255), "i8": (">b", -128, 127), "u16": (">H", 0, 65535), "i16": (">h", -32768, 32767),
        "u32": (">I", 0, 2**32 - 1), "i32": (">i", -(2**31), 2**31 - 1), "sig": (">I", 0, 2**32 - 1)}  # fmt: skip

# Board-specific calibration. The firmware measures these, and a typed-in value would be wrong.
PROTECTED_PREFIXES = ("foc_offsets_",)
# The firmware may keep its own value for these on a write, so they don't count as differences.
IGNORED_IN_COMPARE = ("foc_offsets_",)


def _ignored(field: str) -> bool:
    return field.startswith(IGNORED_IN_COMPARE)


def supported_firmware() -> list[str]:
    names = [f.name for f in resources.files("sky_programmer.layouts").iterdir()]
    return sorted(n[len("fw_layout_"):-len(".json")].replace("_", ".") for n in names if n.endswith(".json"))


class Layout:
    """Field order and types of one firmware's config. There is no fallback to another release."""

    def __init__(self, data: dict):
        self.branch = data["branch"]
        self.signatures = data["signatures"]
        self.enums = data["enums"]
        self.structs = data["structs"]
        self.ops = {MC_ROOT: data["mcconf_ops"], APP_ROOT: data["appconf_ops"]}
        for root, ops in self.ops.items():
            unknown = [o for o in ops if o[0] not in SIZES]
            if unknown:
                raise SafetyRefusal(f"layout {self.branch} has unparsed {root} entries: {unknown[:3]}")
        self.fields = {root: {o[1]: o for o in ops if o[0] != "sig"} for root, ops in self.ops.items()}

    def size(self, root: str) -> int:
        return sum(SIZES[o[0]] for o in self.ops[root])

    def signature(self, root: str) -> int:
        return self.signatures["MCCONF_SIGNATURE" if root == MC_ROOT else "APPCONF_SIGNATURE"]

    def field_type(self, root: str, field: str):
        return self.structs.get(root, {}).get(field.split("[")[0])

    def enum_of(self, root: str, field: str):
        return self.enums.get(self.field_type(root, field))


def load_layout(major: int, minor: int) -> Layout:
    name = f"fw_layout_{major}_{minor:02d}.json"
    try:
        text = resources.files("sky_programmer.layouts").joinpath(name).read_text()
    except FileNotFoundError:
        raise SafetyRefusal(
            f"firmware {major}.{minor:02d} is not supported, so config access is disabled. A wrong layout "
            f"would put values in the wrong fields. Supported: {', '.join(supported_firmware())}."
        ) from None
    return Layout(json.loads(text))


def decode(blob: bytes, layout: Layout, root: str) -> dict:
    """Config bytes (without the command byte) -> {field: value}. Refuses anything unexpected."""
    want = layout.size(root)
    if len(blob) != want:
        raise SafetyRefusal(f"{root} is {len(blob)} bytes but this firmware's layout has {want}. Not using it.")
    r, out = p.Reader(blob), {}
    for kind, field, scale in layout.ops[root]:
        if kind == "sig":
            sig = r.u32()
            if sig != layout.signature(root):
                raise SafetyRefusal(f"{root} signature {sig} does not match firmware layout "
                                    f"{layout.signature(root)}. Not using it.")
        elif kind == "f32auto":
            out[field] = p.dec_f32auto(r.u32())
        elif kind == "f16":
            out[field] = r.i16() / scale
        elif kind == "f32":
            out[field] = r.i32() / scale
        else:
            out[field] = getattr(r, kind)()
    return out


def encode_value(kind: str, scale, val) -> bytes:
    if kind == "f32auto":
        return p.enc_f32auto(val)
    if kind in ("f16", "f32"):
        fmt, lo, hi = (">h", -32768, 32767) if kind == "f16" else (">i", -(2**31), 2**31 - 1)
        scaled = int(round(val * scale))
        if not lo <= scaled <= hi:
            raise SafetyRefusal(f"{val} does not fit this field (its range is {lo / scale:g} to {hi / scale:g}).")
        return struct.pack(fmt, scaled)
    fmt, lo, hi = _INT[kind]
    if not lo <= int(val) <= hi:
        raise SafetyRefusal(f"{val} does not fit this field (its range is {lo} to {hi}).")
    return struct.pack(fmt, int(val))


def patch_blob(blob: bytes, layout: Layout, root: str, changes: dict) -> bytes:
    """Copy of the config with only the named fields re-encoded. Every other byte stays identical."""
    out, off = bytearray(blob), 0
    for kind, field, scale in layout.ops[root]:
        n = SIZES[kind]
        if field in changes:
            out[off:off + n] = encode_value(kind, scale, changes[field])
        off += n
    return bytes(out)


def parse_value(layout: Layout, root: str, field: str, text: str):
    """Turn what the user typed into a value for this field, or refuse."""
    if field.startswith("_") or field not in layout.fields[root]:
        near = [f for f in layout.fields[root] if field.lower() in f.lower()][:6]
        hint = f" Did you mean: {', '.join(near)}?" if near else ""
        raise SafetyRefusal(f"`{field}` is not in this firmware's config.{hint}")
    if field.startswith(PROTECTED_PREFIXES):
        raise SafetyRefusal(f"`{field}` is a calibration value the firmware measures itself. Not setting it.")
    kind = layout.fields[root][field][0]
    text = text.strip()
    if layout.field_type(root, field) == "bool":
        low = text.lower()
        if low in ("1", "true", "on", "yes"):
            return 1
        if low in ("0", "false", "off", "no"):
            return 0
        raise SafetyRefusal(f"`{field}` is on/off, not `{text}`.")
    names = layout.enum_of(root, field)
    if names:
        return _parse_enum(field, names, text)
    if kind in _INT:
        try:
            return int(text, 0)
        except ValueError:
            raise SafetyRefusal(f"`{field}` takes a whole number, not `{text}`.") from None
    try:
        val = float(text)
    except ValueError:
        raise SafetyRefusal(f"`{field}` takes a number, not `{text}`.") from None
    if not math.isfinite(val):
        raise SafetyRefusal(f"`{field}` must be a finite number, not `{text}`.")
    return val


def _parse_enum(field: str, names: dict, text: str) -> int:
    upper = text.upper()
    if text.lstrip("-").isdigit() and text in names:
        return int(text)
    hits = [int(k) for k, n in names.items() if n.upper() == upper or n.upper().endswith("_" + upper)]
    if len(hits) == 1:
        return hits[0]
    raise SafetyRefusal(f"`{field}` cannot be `{text}`. Choices: {', '.join(names.values())}.")


def show(layout: Layout, root: str, field: str, val) -> str:
    names = layout.enum_of(root, field)
    if names and str(val) in names:
        return names[str(val)]
    return f"{val:.6g}" if isinstance(val, float) else str(val)


def differences(before: dict, after: dict) -> dict:
    """{field: (before, after)} for fields that really differ."""
    out = {}
    for k, a in before.items():
        b = after.get(k)
        if _ignored(k) or a == b:
            continue
        if isinstance(a, float) and isinstance(b, float) and math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9):
            continue
        out[k] = (a, b)
    return out


def _rule(bad: bool, text: str):
    return [text] if bad else []


def motor_rule_violations(c: dict, layout: Layout) -> set:
    """Values no working setup uses. They catch typos and wizard damage (the flux = -2 case)."""
    foc = layout.enum_of(MC_ROOT, "motor_type").get(str(c["motor_type"])) == "MOTOR_TYPE_FOC"
    out = []
    out += _rule(c["si_motor_poles"] <= 0 or c["si_motor_poles"] % 2, "si_motor_poles must be even and above 0")
    out += _rule(c["l_current_max"] <= 0, "l_current_max must be above 0")
    out += _rule(c["l_current_min"] > 0, "l_current_min must be 0 or negative")
    out += _rule(c["l_in_current_max"] <= 0, "l_in_current_max must be above 0")
    out += _rule(c["l_in_current_min"] > 0, "l_in_current_min must be 0 or negative")
    out += _rule(c["l_abs_current_max"] < c["l_current_max"], "l_abs_current_max must not be below l_current_max")
    out += _rule(c["l_min_vin"] >= c["l_max_vin"], "l_min_vin must be below l_max_vin")
    out += _rule(c["l_battery_cut_end"] > c["l_battery_cut_start"],
                 "l_battery_cut_end must not be above l_battery_cut_start")
    out += _rule(not 0 < c["l_max_duty"] <= 1, "l_max_duty must be above 0 and at most 1")
    out += _rule(c["l_min_duty"] >= c["l_max_duty"], "l_min_duty must be below l_max_duty")
    out += _rule(c["m_bldc_f_sw_min"] > c["m_bldc_f_sw_max"], "m_bldc_f_sw_min must not be above m_bldc_f_sw_max")
    out += _rule(c["l_temp_fet_start"] > c["l_temp_fet_end"], "l_temp_fet_start must not be above l_temp_fet_end")
    out += _rule(c["l_temp_motor_start"] > c["l_temp_motor_end"],
                 "l_temp_motor_start must not be above l_temp_motor_end")
    for f in ("foc_motor_r", "foc_motor_l", "foc_motor_flux_linkage"):
        out += _rule(c[f] < 0, f"{f} must not be negative")
    if foc:
        for f in ("foc_motor_r", "foc_motor_l", "foc_motor_flux_linkage", "foc_current_kp", "foc_current_ki",
                  "foc_f_zv"):  # fmt: skip
            out += _rule(c[f] <= 0, f"FOC needs {f} above 0")
    return set(out)
