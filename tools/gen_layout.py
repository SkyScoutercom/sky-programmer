#!/usr/bin/env python3
"""Generate src/sky_programmer/layouts/fw_layout_<major>_<minor>.json from that firmware's sources.

The sources are fetched as text and parsed; nothing from them is executed. The app itself never
touches the network: a firmware is supported only once its layout is reviewed and committed.

Usage: python3 tools/gen_layout.py 7 0
"""
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "sky_programmer", "layouts")


def fetch(branch, path):
    url = f"https://raw.githubusercontent.com/vedderb/bldc/{branch}/{path}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def fetch_tool(major, minor):
    url = f"https://raw.githubusercontent.com/vedderb/vesc_tool/master/res/config/{major}.{minor:02d}/parameters_mcconf.xml"
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def tool_limits(xml_text):
    """{field: [min, max]} of the numeric motor-config fields, from VESC Tool's own parameter file."""
    out = {}
    for e in ET.fromstring(xml_text).find("Params"):
        d = {c.tag: c.text for c in e}
        lo, hi = d.get("minDouble") or d.get("minInt"), d.get("maxDouble") or d.get("maxInt")
        if lo is not None and hi is not None and d.get("type") in ("1", "2"):
            name = e.tag
            if "__" in name:
                base, idx = name.rsplit("__", 1)
                name = f"{base}[{idx}]"
            out[name] = [float(lo), float(hi)]
    return out


def serialize_ops(src, func):
    """Ordered [kind, field, scale] from a confgenerator serialize function."""
    body = re.search(func + r"\s*\([^)]*\)\s*\{(.*?)\n\}", src, re.S).group(1)
    ops = []
    for ln in (x.strip() for x in body.split("\n")):
        if ln.startswith("buffer_append_uint32(buffer, ") and "SIGNATURE" in ln:
            ops.append(["sig", None, None])
        elif m := re.match(r"buffer\[ind\+\+\]\s*=\s*(?:\((u?int8_t)\))?\s*conf->([\w\.\[\]]+);", ln):
            ops.append(["i8" if m.group(1) == "int8_t" else "u8", m.group(2), None])
        elif m := re.match(r"buffer_append_float32_auto\(buffer, conf->([\w\.\[\]]+), &ind\);", ln):
            ops.append(["f32auto", m.group(1), None])
        elif m := re.match(r"buffer_append_float(16|32)\(buffer, conf->([\w\.\[\]]+), ([\deE\.\-]+), &ind\);", ln):
            ops.append(["f" + m.group(1), m.group(2), float(m.group(3))])
        elif m := re.match(r"buffer_append_(u?int)(16|32)\(buffer, (?:\(\w+\))?conf->([\w\.\[\]]+), &ind\);", ln):
            ops.append([("u" if m.group(1) == "uint" else "i") + m.group(2), m.group(3), None])
        elif ln and not ln.startswith(("int32_t ind", "return", "//", "}")):
            ops.append(["??", ln, None])  # the app refuses a layout containing this
    return ops


def enums_and_structs(dt):
    enums = {}
    for m in re.finditer(r"typedef enum\s*\{(.*?)\}\s*(\w+);", dt, re.S):
        names, v = {}, -1
        for item in re.sub(r"//[^\n]*|/\*.*?\*/", "", m.group(1), flags=re.S).split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                n, val = [x.strip() for x in item.split("=", 1)]
                try:
                    v = int(val, 0)
                except ValueError:
                    v += 1
            else:
                n, v = item, v + 1
            names[v] = n
        enums[m.group(2)] = names
    structs = {}
    for m in re.finditer(r"typedef struct\s*\w*\s*\{(.*?)\}\s*(\w+);", dt, re.S):
        mem = {}
        for mm in re.finditer(r"^\s*(\w+)\s+(\w+)(\[[^\]]*\])?\s*;", re.sub(r"//[^\n]*", "", m.group(1)), re.M):
            mem[mm.group(2)] = mm.group(1)
        structs[m.group(2)] = mem
    return enums, structs


def build_layout(branch, major, minor):
    cg, cgh, dt = fetch(branch, "confgenerator.c"), fetch(branch, "confgenerator.h"), fetch(branch, "datatypes.h")
    enums, structs = enums_and_structs(dt)
    ext = fetch(branch, "lispBM/lispif_vesc_extensions.c")
    return {
        "branch": branch,
        "signatures": {k: int(v) for k, v in re.findall(r"#define\s+(\w+_SIGNATURE)\s+(\d+)", cgh)},
        "mcconf_ops": serialize_ops(cg, "confgenerator_serialize_mcconf"),
        "appconf_ops": serialize_ops(cg, "confgenerator_serialize_appconf"),
        "enums": {name: {str(k): v for k, v in vals.items()} for name, vals in enums.items()},
        "structs": structs,
        "mc_limits": tool_limits(fetch_tool(major, minor)),
        "lisp_extensions": sorted(set(re.findall(r'lbm_add_extension\("([^"]+)"', ext))),
    }


def main():
    major, minor = int(sys.argv[1]), int(sys.argv[2])
    lay = build_layout(f"release_{major}_{minor:02d}", major, minor)
    path = os.path.join(OUT, f"fw_layout_{major}_{minor:02d}.json")
    with open(path, "w") as f:
        json.dump(lay, f, indent=1)
    print("wrote", path)


if __name__ == "__main__":
    main()
