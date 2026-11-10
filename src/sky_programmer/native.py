"""Building a script's native (C) libraries from source, and checking them, before they're stored.

A native library is machine code with full access to the board, so this is a guard against mistakes, not a sandbox:
- it builds in a temporary copy with the VESC build rules (lispBM/c_libs/rules.mk), never in the project folder;
- the interface files must be pinned to the board's own firmware commit: a mismatched vesc_c_if.h puts every call at
  the wrong place in the function table;
- the source may not call what writes flash or sends raw firmware commands, and calls that drive the motor or change
  the config warn;
- the built ELF must need no relocation, because the loader runs it in place without fixing up anything
  (lispif_c_lib.c): no GOT, no .data or .bss, and no global symbol but init;
- it says whether the committed .bin matches a fresh build of its source.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SafetyRefusal

TOOLCHAIN_VERSION = "13.3.1"  # Arm GNU Toolchain 13.3.rel1: byte-identical builds need exactly this one
TOOLCHAIN_DIR_NAME = "arm-gnu-toolchain-13.3.rel1-x86_64-arm-none-eabi"
BUILD_TIMEOUT_S = 300

# vesc_c_if.h members a library may not call, and why.
REFUSED_CALLS = {
    "commands_process_packet": "it sends raw firmware commands, erasing the bootloader among them",
    "write_nvm": "it writes flash", "wipe_nvm": "it erases flash, the bootloader sector included",
    "store_cfg": "it stores the config to flash without the programmer's checks",
    "store_backup_data": "it writes flash",
    "can_send_buffer": "it sends to other boards on CAN, firmware commands included",
}  # fmt: skip
# Allowed, with a warning.
WARNED_CALLS = {
    "set_cfg_float": "it changes the config in RAM", "set_cfg_int": "it changes the config in RAM",
    "conf_custom_add_config": "it adds a custom config that VESC Tool can write",
    "foc_set_openloop_current": "it drives the motor", "foc_set_openloop_phase": "it drives the motor",
    "foc_set_openloop_duty": "it drives the motor", "foc_set_openloop_duty_phase": "it drives the motor",
    "mc_set_current": "it drives the motor", "mc_set_duty": "it drives the motor", "mc_set_rpm": "it drives the motor",
}  # fmt: skip
ALLOWED_GLOBALS = {"init", "_text"}


@dataclass
class Project:
    folder: Path
    target: str
    c_libs: Path  # the lispBM/c_libs folder the Makefile includes rules.mk from


@dataclass
class Build:
    name: str
    data: bytes
    version: str
    commit: str | None
    matches_committed: bool | None  # None: no committed .bin to compare with
    warnings: list = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


# ---------------------------------------------------------------------- finding the project and the toolchain
def find_project(bin_path) -> Project | None:
    """The Makefile project that builds this .bin, if its source sits next to it, else None (a prebuilt file)."""
    bin_path = Path(bin_path)
    makefile = bin_path.parent / "Makefile"
    if not makefile.is_file():
        return None
    text = makefile.read_text(errors="replace")
    target = re.search(r"^\s*TARGET\s*[:?]?=\s*(\S+)", text, re.M)
    if not target or target.group(1) != bin_path.stem or "rules.mk" not in text:
        return None
    path = re.search(r"^\s*VESC_C_LIB_PATH\s*[:?]?=\s*(\S+)", text, re.M)
    if not path:
        raise SafetyRefusal(f"{makefile} doesn't set VESC_C_LIB_PATH, so the VESC build files can't be found.")
    c_libs = (bin_path.parent / path.group(1)).resolve()
    if not (c_libs / "vesc_c_if.h").is_file() or not (c_libs / "rules.mk").is_file():
        raise SafetyRefusal(f"the VESC build files aren't in {c_libs}. Fetch them first (fetch_c_libs.sh).")
    return Project(bin_path.parent, bin_path.stem, c_libs)


def toolchain_candidates() -> list[Path]:
    out = []
    if os.environ.get("SKY_ARM_TOOLCHAIN"):
        root = Path(os.environ["SKY_ARM_TOOLCHAIN"]).expanduser()
        out += [root / "bin", root]
    on_path = shutil.which("arm-none-eabi-gcc")
    if on_path:
        out.append(Path(on_path).parent)
    out.append(Path.home() / ".local" / "opt" / TOOLCHAIN_DIR_NAME / "bin")
    return out


def find_toolchain() -> tuple[Path, str]:
    """(bin folder, gcc version) of the Arm toolchain."""
    for folder in toolchain_candidates():
        gcc = folder / "arm-none-eabi-gcc"
        if gcc.is_file() and os.access(gcc, os.X_OK):
            version = subprocess.run([str(gcc), "-dumpversion"], capture_output=True, text=True,
                                     timeout=30).stdout.strip()  # fmt: skip
            return folder, version
    raise SafetyRefusal(f"the Arm toolchain isn't installed. Install Arm GNU Toolchain 13.3.rel1 into "
                        f"~/.local/opt/{TOOLCHAIN_DIR_NAME}, or point SKY_ARM_TOOLCHAIN at it.")  # fmt: skip


def interface_commit(c_libs: Path) -> str | None:
    """The firmware commit the interface files were fetched at (fetch_c_libs.sh makes them a git checkout)."""
    try:
        out = subprocess.run(["git", "-C", str(c_libs), "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=30)  # fmt: skip
    except OSError:
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", commit) else None


def board_commit(prog) -> str | None:
    """The firmware commit the board reports in `fw_info`, e.g. 'Git Hash  : 20cbb36…'."""
    for line in prog.terminal("fw_info", wait=1.0):
        found = re.search(r"Git Hash\s*:\s*([0-9a-f]{7,40})", line)
        if found:
            return found.group(1)
    return None


def require_matching_interface(build_commit: str | None, firmware_commit: str | None) -> None:
    if not build_commit or not firmware_commit:
        raise SafetyRefusal(f"can't tell whether the library's interface files ({build_commit or 'unknown commit'}) "
                            f"match the board's firmware ({firmware_commit or 'unknown commit'}). A mismatch calls "
                            "the wrong firmware functions.")  # fmt: skip
    if not build_commit.startswith(firmware_commit) and not firmware_commit.startswith(build_commit):
        raise SafetyRefusal(f"the library is built against firmware commit {build_commit[:8]}, but the board runs "
                            f"{firmware_commit[:8]}. A mismatched vesc_c_if.h calls the wrong firmware functions: "
                            "re-pin the interface files to the board's commit and build again.")  # fmt: skip


# ---------------------------------------------------------------------- checking the source and the result
def check_sources(project: Project) -> list[str]:
    """Refuse calls that write flash or send raw commands; warn on ones that drive the motor or change the config.

    It reads the project's own .c and .h files only. A call made some other way, through pointer arithmetic for
    example, isn't seen: this catches mistakes, not intent.
    """
    warnings, refused = [], []
    for path in sorted(project.folder.glob("*.[ch]")):
        text = path.read_text(errors="replace")
        for name, why in REFUSED_CALLS.items():
            if re.search(rf"\b{name}\b", text):
                refused.append(f"{path.name} uses `{name}`: {why}")
        for name, why in WARNED_CALLS.items():
            if re.search(rf"\b{name}\b", text):
                warnings.append(f"{path.name} uses `{name}`: {why}.")
    if refused:
        raise SafetyRefusal(f"the library {project.target} can't be stored: " + "; ".join(refused) + ".")
    return warnings


def elf_problems(sections: dict, global_symbols: set) -> list[str]:
    """What would break when the loader runs the library in place, from `objdump -h` sizes and `nm` globals."""
    problems = []
    if sections.get(".got", 0):
        problems.append("it has GOT entries, which the VESC loader never fills in")
    if sections.get(".data", 0) or sections.get(".bss", 0):
        problems.append("it has .data or .bss, but a stored library runs from flash, with no writable statics")
    if sections.get(".program_ptr") != 4:
        problems.append("it has no 4-byte .program_ptr section, which the loader expects first")
    extra = sorted(global_symbols - ALLOWED_GLOBALS)
    if extra:
        problems.append(f"it has global symbols besides init ({', '.join(extra)}): make them static")
    return problems


def _sections(objdump_h: str) -> dict:
    return {m.group(1): int(m.group(2), 16)
            for m in re.finditer(r"^\s*\d+\s+(\S+)\s+([0-9a-f]+)", objdump_h, re.M)}  # fmt: skip


def _globals(nm: str) -> set:
    return {line.split()[-1] for line in nm.splitlines() if re.match(r"^\S+ [A-Z] ", line)}


# ---------------------------------------------------------------------- building
def build(project: Project, toolchain: Path | None = None, version: str | None = None) -> Build:
    """Build in a temporary copy of the project and of the VESC build files, check it, and return the .bin."""
    warnings = check_sources(project)
    if toolchain is None:
        toolchain, version = find_toolchain()
    if version != TOOLCHAIN_VERSION:
        warnings.append(f"the toolchain is gcc {version}, not {TOOLCHAIN_VERSION} (Arm GNU Toolchain 13.3.rel1), so "
                        "the result won't be byte-identical to a build made with it.")  # fmt: skip
    env = {**os.environ, "PATH": f"{toolchain}{os.pathsep}{os.environ.get('PATH', '')}"}
    with tempfile.TemporaryDirectory(prefix="sky-build-") as tmp:
        work, c_libs = Path(tmp) / project.target, Path(tmp) / "c_libs"
        shutil.copytree(project.c_libs, c_libs, ignore=shutil.ignore_patterns("*.so", ".git"))
        work.mkdir()
        for path in project.folder.iterdir():  # sources and the Makefile only: never old build outputs
            if path.is_file() and (path.suffix in (".c", ".h") or path.name == "Makefile"):
                shutil.copy2(path, work / path.name)
        try:
            run = subprocess.run(["make", "-s", f"VESC_C_LIB_PATH={c_libs}/"], cwd=work, env=env,
                                 capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SafetyRefusal(f"building {project.target} failed: {e}") from e
        if run.returncode != 0:
            tail = "\n".join((run.stdout + run.stderr).strip().splitlines()[-12:])
            raise SafetyRefusal(f"building {project.target} failed:\n{tail}")
        elf, built = work / f"{project.target}.elf", work / f"{project.target}.bin"
        sections = _sections(_tool(toolchain, "objdump", "-h", elf))
        global_symbols = _globals(_tool(toolchain, "nm", elf))
        problems = elf_problems(sections, global_symbols)
        if problems:
            raise SafetyRefusal(f"the built library {project.target} can't run on the board: " + "; ".join(problems)
                                + ".")  # fmt: skip
        data = built.read_bytes()
    committed = project.folder / f"{project.target}.bin"
    same = committed.read_bytes() == data if committed.is_file() else None
    return Build(project.target, data, version or "", interface_commit(project.c_libs), same, warnings)


def _tool(toolchain: Path, name: str, *args) -> str:
    out = subprocess.run([str(toolchain / f"arm-none-eabi-{name}"), *map(str, args)], capture_output=True, text=True,
                         timeout=60)  # fmt: skip
    if out.returncode != 0:
        raise SafetyRefusal(f"arm-none-eabi-{name} failed: {out.stderr.strip()[:200]}")
    return out.stdout


def build_imports(text: str, folder) -> list[tuple[str, str, Build | None]]:
    """(import path, name, build or None if prebuilt) for each import of a script."""
    from .code import find_imports

    out = []
    for _, path, name in find_imports(text):
        bin_path = next((f for f in (Path(folder) / path, Path(path)) if f.is_file()), Path(folder) / path)
        project = find_project(bin_path)
        out.append((path, name, build(project) if project else None))
    return out
