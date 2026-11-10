"""Building a script's native (C) libraries from source, and checking them, before they're stored.

A native library is machine code with full access to the board, so this is a guard against mistakes, not a sandbox.
Building runs the project's Makefile, so only build projects you trust.
- It builds in a temporary copy, with the VESC build rules (lispBM/c_libs/rules.mk) and a minimal environment.
- The interface files must be an unmodified checkout pinned to the board's own firmware commit: a different or edited
  vesc_c_if.h shifts the function table, and one slot turns read_nvm into write_nvm and write_nvm into wipe_nvm.
- The source may not reach flash, reset the board or send raw firmware commands; motor, CAN and pin calls warn.
- The loader (lispif_c_lib.c) runs the library in place from flash, calling base + 4, with nothing relocated. So the
  ELF must have .program_ptr at 0 and init at 4, and no writable data: a static variable lands in flash, and a
  pointer table holds addresses counted from 0, which point into the firmware.
- The stored bytes are made here from the checked ELF, and the report says whether the committed .bin matches.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
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
    "store_backup_data": "it writes flash", "store_eeprom_var": "it writes the emulated EEPROM, which holds the config",
    "can_send_buffer": "it sends to other boards on CAN, firmware commands included",
}  # fmt: skip
# Other things a library's source may not do, as patterns.
REFUSED_PATTERNS = {
    r"\bFLASH\s*->": "it programs the flash controller directly",
    r"\bFLASH_\w+\s*\(": "it calls the flash driver",
    r"\bOB_\w+": "it touches the option bytes",
    r"\bOPTCR\b": "it touches the option bytes",
    r"\bNVIC_SystemReset\b": "it resets the board",
    r"\bAIRCR\b": "it can reset the board",
    r"->\s*(set_cfg|get_cfg|get_cfg_xml)\s*\(": "firmware 7.00 leaves this pointer unset, so the call jumps to 0",
}  # fmt: skip
# Allowed, with a warning.
WARNED_CALLS = {
    **{name: "it changes the config in RAM" for name in ("set_cfg_float", "set_cfg_int")},
    "conf_custom_add_config": "it adds a custom config that VESC Tool can write",
    **{name: "it drives the motor" for name in (
        "foc_set_openloop_current", "foc_set_openloop_phase", "foc_set_openloop_duty", "foc_set_openloop_duty_phase",
        "mc_set_current", "mc_set_current_rel", "mc_set_duty", "mc_set_duty_noramp", "mc_set_rpm",
        "mc_set_brake_current", "mc_set_brake_current_rel", "mc_set_handbrake", "mc_set_handbrake_rel",
        "mc_set_pid_speed", "mc_set_pid_pos", "mc_set_pwm_callback", "foc_set_fw_override", "foc_beep",
        "foc_play_tone", "foc_play_audio_samples", "encoder_set_custom_callbacks")},
    **{name: "it drives other boards' motors on CAN" for name in (
        "can_set_current", "can_set_current_rel", "can_set_duty", "can_set_rpm", "can_set_pos",
        "can_set_current_brake", "can_set_current_brake_rel")},
    **{name: "it sends raw CAN frames, which can carry firmware commands" for name in (
        "can_transmit_sid", "can_transmit_eid")},
    **{name: "it writes to a serial port" for name in ("uart_write", "packet_send_packet")},
    **{name: "it changes a pin's mode: on a gate-driver pin that damages the board" for name in (
        "set_pad_mode", "io_set_mode")},
}  # fmt: skip
ALLOWED_GLOBALS = {"init", "_text"}
BAD_SECTION_PREFIXES = (".data", ".bss", ".got", ".init_array", ".fini_array", ".preinit_array")


@dataclass
class Project:
    folder: Path
    target: str
    c_libs: Path  # the lispBM/c_libs folder the Makefile includes rules.mk from


@dataclass
class Section:
    name: str
    addr: int
    size: int
    flags: str


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
    for sources in re.findall(r"^\s*SOURCES\s*[:+?]?=(.*)$", text, re.M):
        for item in sources.split():
            if item.startswith(("/", "~")) or ".." in Path(item).parts or "$" in item:
                raise SafetyRefusal(f"{makefile} builds {item}, outside the project folder. Only sources inside it "
                                    "are checked and copied, so move it in.")  # fmt: skip
    path = re.search(r"^\s*VESC_C_LIB_PATH\s*[:?]?=\s*(\S+)", text, re.M)
    if not path:
        raise SafetyRefusal(f"{makefile} doesn't set VESC_C_LIB_PATH, so the VESC build files can't be found.")
    c_libs = (bin_path.parent / path.group(1)).resolve()
    if not (c_libs / "vesc_c_if.h").is_file() or not (c_libs / "rules.mk").is_file():
        raise SafetyRefusal(f"the VESC build files aren't in {c_libs}. Fetch them first (fetch_c_libs.sh).")
    return Project(bin_path.parent, bin_path.stem, c_libs)


def toolchain_candidates() -> list[Path]:
    """SKY_ARM_TOOLCHAIN, then the pinned version in ~/.local/opt, then whatever is on PATH."""
    out = []
    if os.environ.get("SKY_ARM_TOOLCHAIN"):
        root = Path(os.environ["SKY_ARM_TOOLCHAIN"]).expanduser()
        out += [root / "bin", root]
    out.append(Path.home() / ".local" / "opt" / TOOLCHAIN_DIR_NAME / "bin")
    on_path = shutil.which("arm-none-eabi-gcc")
    if on_path:
        out.append(Path(on_path).parent)
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


def _git(c_libs: Path, *args) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(c_libs), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None


def interface_commit(c_libs: Path) -> str | None:
    """The firmware commit the interface files were fetched at (fetch_c_libs.sh makes them a git checkout)."""
    out = _git(c_libs, "rev-parse", "HEAD")
    commit = out.stdout.strip() if out and out.returncode == 0 else ""
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None


def interface_changes(c_libs: Path) -> list[str]:
    """Tracked files under c_libs changed since that commit. Untracked build outputs (*.so) don't count."""
    out = _git(c_libs, "status", "--porcelain", "--untracked-files=no", "--", ".")
    if out is None or out.returncode != 0:
        return ["can't check the interface files with git"]
    return [line[3:] for line in out.stdout.splitlines() if line.strip()]


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
    """Refuse what reaches flash, resets the board or sends raw commands; warn on motor, CAN, serial and pin calls.

    It reads the project's own .c and .h files as text. Something done another way, through pointer arithmetic for
    example, isn't seen: this catches mistakes, not intent.
    """
    warnings, refused = [], []
    for path in sorted(project.folder.glob("*.[ch]")):
        text = path.read_text(errors="replace")
        for name, why in REFUSED_CALLS.items():
            if re.search(rf"\b{name}\b", text):
                refused.append(f"{path.name} uses `{name}`: {why}")
        for pattern, why in REFUSED_PATTERNS.items():
            found = re.search(pattern, text)
            if found:
                refused.append(f"{path.name} has `{found.group(0).strip()}`: {why}")
        for name, why in WARNED_CALLS.items():
            if re.search(rf"\b{name}\b", text):
                warnings.append(f"{path.name} uses `{name}`: {why}.")
    if refused:
        raise SafetyRefusal(f"the library {project.target} can't be stored: " + "; ".join(refused) + ".")
    return warnings


def elf_problems(sections: list[Section], symbols: dict) -> list[str]:
    """What would break when the loader runs the library in place, from `readelf -S -W` and `nm` globals."""
    problems = []
    loaded = [s for s in sections if "A" in s.flags and s.size]
    for s in loaded:
        if s.name.startswith(BAD_SECTION_PREFIXES) or ("W" in s.flags and s.name != ".program_ptr"):
            problems.append(f"it has writable data in {s.name}: a stored library runs from flash, so static variables "
                            "can't change, and pointer tables hold addresses counted from 0, which point into the "
                            "firmware. Keep state in memory from VESC_IF->malloc")  # fmt: skip
    by_name = {s.name: s for s in loaded}
    ptr, init_fun = by_name.get(".program_ptr"), by_name.get(".init_fun")
    if ptr is None or ptr.addr != 0 or ptr.size != 4:
        problems.append("it has no 4-byte .program_ptr at address 0, which the loader expects first")
    if init_fun is None or init_fun.addr != 4:
        problems.append("its init isn't in .init_fun at address 4, where the loader jumps: declare it with INIT_FUN")
    if symbols.get("init", -1) & ~1 != 4:
        problems.append("its init symbol isn't at address 4, where the loader jumps")
    extra = sorted(set(symbols) - ALLOWED_GLOBALS)
    if extra:
        problems.append(f"it has global symbols besides init ({', '.join(extra)}): make your functions static. "
                        "Compiler helpers such as __aeabi_* come from 64-bit division and similar")  # fmt: skip
    return problems


def _sections(readelf: str) -> list[Section]:
    out = []
    for line in readelf.splitlines():
        m = re.match(r"\s*\[\s*\d+\]\s+(\S+)\s+\S+\s+([0-9a-f]+)\s+[0-9a-f]+\s+([0-9a-f]+)\s+\S+\s+([A-Za-z]*)\s", line)
        if m and m.group(1).startswith("."):
            out.append(Section(m.group(1), int(m.group(2), 16), int(m.group(3), 16), m.group(4)))
    return out


def _globals(nm: str) -> dict:
    out = {}
    for line in nm.splitlines():
        m = re.match(r"^([0-9a-f]+) [A-Z] (\S+)$", line)
        if m:
            out[m.group(2)] = int(m.group(1), 16)
    return out


def _load_matches_run(readelf_l: str) -> bool:
    """Every LOAD segment is loaded where it runs (physical address = virtual address)."""
    loads = re.findall(r"^\s*LOAD\s+\S+\s+(0x[0-9a-f]+)\s+(0x[0-9a-f]+)", readelf_l, re.M)
    return bool(loads) and all(v == p for v, p in loads)


# ---------------------------------------------------------------------- building
def build(project: Project, toolchain: Path | None = None, version: str | None = None) -> Build:
    """Build in a temporary copy of the project and of the VESC build files, check it, and return its bytes."""
    warnings = check_sources(project)
    changed = interface_changes(project.c_libs)
    if changed:
        raise SafetyRefusal(f"the VESC interface files in {project.c_libs} have local changes ({', '.join(changed)}). "
                            "A changed vesc_c_if.h calls the wrong firmware functions: restore them, for example "
                            "by running fetch_c_libs.sh again.")  # fmt: skip
    if toolchain is None:
        toolchain, version = find_toolchain()
    if version != TOOLCHAIN_VERSION:
        warnings.append(f"the toolchain is gcc {version}, not {TOOLCHAIN_VERSION} (Arm GNU Toolchain 13.3.rel1), so "
                        "the result won't be byte-identical to a build made with it.")  # fmt: skip
    env = {"PATH": f"{toolchain}{os.pathsep}/usr/local/bin{os.pathsep}/usr/bin{os.pathsep}/bin",
           "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C", "LC_ALL": "C"}  # fmt: skip
    with tempfile.TemporaryDirectory(prefix="sky-build-") as tmp:
        work, c_libs = Path(tmp) / project.target, Path(tmp) / "c_libs"
        shutil.copytree(project.c_libs, c_libs, symlinks=False, ignore=shutil.ignore_patterns("*.so", ".git"))
        work.mkdir()
        for path in project.folder.iterdir():  # sources and the Makefile only: never old build outputs
            if path.is_file() and (path.suffix in (".c", ".h") or path.name == "Makefile"):
                shutil.copyfile(path, work / path.name)
        _make(work, env, project.target, c_libs)
        elf, made = work / f"{project.target}.elf", work / f"{project.target}.bin"
        problems = elf_problems(_sections(_tool(toolchain, "readelf", "-S", "-W", elf)),
                                _globals(_tool(toolchain, "nm", elf)))  # fmt: skip
        if not _load_matches_run(_tool(toolchain, "readelf", "-l", "-W", elf)):
            problems.append("it isn't loaded where it runs")
        if problems:
            raise SafetyRefusal(f"the built library {project.target} can't run on the board: " + "; ".join(problems)
                                + ".")  # fmt: skip
        ours = Path(tmp) / "checked.bin"  # the bytes stored are made from the ELF that was checked
        _tool(toolchain, "objcopy", "-O", "binary", "--gap-fill", "0x00", elf, ours)
        data = ours.read_bytes()
        if not made.is_file() or made.read_bytes() != data:
            raise SafetyRefusal(f"the Makefile's {made.name} differs from the checked ELF, so it isn't stored.")
    committed = project.folder / f"{project.target}.bin"
    same = committed.read_bytes() == data if committed.is_file() else None
    return Build(project.target, data, version or "", interface_commit(project.c_libs), same, warnings)


def _make(work: Path, env: dict, target: str, c_libs: Path) -> None:
    """Run the build, killing everything it started if it hangs."""
    cmd = ["make", "-s", f"VESC_C_LIB_PATH={c_libs}/", target]
    try:
        proc = subprocess.Popen(cmd, cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)  # fmt: skip
    except OSError as e:
        raise SafetyRefusal(f"building {target} failed: {e}") from e
    try:
        output, _ = proc.communicate(timeout=BUILD_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise SafetyRefusal(f"building {target} took more than {BUILD_TIMEOUT_S} s and was stopped.") from e
    if proc.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-12:])
        raise SafetyRefusal(f"building {target} failed:\n{tail}")


def _tool(toolchain: Path, name: str, *args) -> str:
    out = subprocess.run([str(toolchain / f"arm-none-eabi-{name}"), *map(str, args)], capture_output=True, text=True,
                         timeout=60)  # fmt: skip
    if out.returncode != 0:
        raise SafetyRefusal(f"arm-none-eabi-{name} failed: {out.stderr.strip()[:200]}")
    return out.stdout


def report_lines(builds) -> list[str]:
    """What each import's build gave, worded the same in the terminal and the desktop app."""
    lines = []
    for path, name, result in builds:
        if result is None:
            lines.append(f"{name}: {path} has no source and Makefile next to it, so there's nothing to build.")
            continue
        commit = result.commit[:8] if result.commit else "an unknown commit"
        lines.append(f"{name}: built from source, {len(result.data)} bytes, SHA-256 {result.sha256}, gcc "
                     f"{result.version}, interface files at {commit}.")  # fmt: skip
        if result.matches_committed is False:
            lines.append(f"  The committed {path} differs from this build of its source.")
        elif result.matches_committed:
            lines.append(f"  The committed {path} matches this build byte for byte.")
        lines += [f"  Warning: {warning}" for warning in result.warnings]
    return lines


def fresh_builds_for_board(prog, builds) -> dict:
    """{import path: built bytes}, once every import was built and all match the board's firmware commit."""
    prebuilt = [path for path, _, result in builds if result is None]
    if prebuilt:
        raise SafetyRefusal(f"building from source was asked for, but {', '.join(prebuilt)} has no source to build "
                            "from. Store without building to use the file as it is.")  # fmt: skip
    firmware = board_commit(prog)
    for _, _, result in builds:
        require_matching_interface(result.commit, firmware)
    return {path: result.data for path, _, result in builds}


def build_imports(text: str, folder) -> list[tuple[str, str, Build | None]]:
    """(import path, name, build or None if prebuilt) for each import of a script."""
    from .code import find_imports

    out = []
    for _, path, name in find_imports(text):
        bin_path = next((f for f in (Path(folder) / path, Path(path)) if f.is_file()), Path(folder) / path)
        project = find_project(bin_path)
        out.append((path, name, build(project) if project else None))
    return out
