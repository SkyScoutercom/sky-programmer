"""The `sky` command."""
from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import sys
import time
from pathlib import Path

from . import APP_NAME, __version__, backup, code, config, native
from .config import APP_ROOT, MC_ROOT
from .errors import SafetyRefusal, SkyError
from .link import Link, find_port, list_ports, open_serial
from .programmer import MONITOR_COLUMNS, Programmer

ENGINE_PROMPT = "Engine stopped, propeller clear, supply stable? Type YES to write: "

# The two blues of the SkyScouter logo (docs/logo.png).
LOGO_LIGHT, LOGO_DARK = "38;2;2;149;224", "38;2;0;85;142"


def _banner() -> None:
    """Show the brand in a real terminal only, so piped output and logs stay plain. Honours NO_COLOR."""
    if not sys.stdout.isatty():
        return
    if os.environ.get("NO_COLOR") or sys.platform == "win32":  # an older Windows console prints the codes as text
        print(APP_NAME)
    else:
        print(f"\033[1;{LOGO_LIGHT}mS\033[1;{LOGO_DARK}mK\033[0m  {APP_NAME} {__version__}  |  SkyScouter")


def _confirm(args, prompt=ENGINE_PROMPT) -> None:
    """The operator states the engine is stopped: the board can't see it behind the starter relay."""
    if args.engine_stopped:
        return
    if not sys.stdin.isatty():
        raise SafetyRefusal("this command needs --engine-stopped when it is not run interactively.")
    try:
        answer = input(prompt)
    except EOFError:
        answer = ""
    if answer.strip() != "YES":
        raise SafetyRefusal("cancelled. Nothing was written.")


def _print_plan(prog, plan) -> None:
    lay = prog.layout()
    print("Planned changes:")
    for k, (a, b) in sorted(plan.diff.items()):
        left, right = config.show_pair(lay, MC_ROOT, k, a, b)
        print(f"  {k}: {left} -> {right}")
    for w in plan.warnings:
        print(f"Warning: {w}")


def _commit(prog, plan, args) -> int:
    _print_plan(prog, plan)
    if args.dry_run:
        print("Dry run: nothing was written.")
        return 0
    _confirm(args)
    res = prog.commit(plan)
    lay = prog.layout()
    for k, (want, got) in res["limited"].items():
        print(f"Limited by firmware: {k} asked {config.show(lay, MC_ROOT, k, want)}, "
              f"stored {config.show(lay, MC_ROOT, k, got)}")
    print("Written. The board's working copy reads back correct.")
    print("The firmware doesn't report whether its flash save worked, so power-cycle and check with `sky get`")
    print("to confirm a change you depend on.")
    print("The script is paused. Run `sky start` or power-cycle to run it again.")
    return 0


def cmd_status(prog, args) -> int:
    _banner()
    ident = prog.identify()
    print(f"Board:    {ident['hw']}")
    print(f"Firmware: {ident['fw']}" + (f" (test build {ident['test_version']})" if ident["test_version"] else ""))
    print(f"UUID:     {ident['uuid']}")
    v = prog.values()
    print(f"Bus:      {v['v_in']} V    Motor: {v['erpm']} erpm, {v['current_motor']} A, duty {v['duty']}")
    print(f"Temps:    MOSFET {v['temp_fet']} C, motor {v['temp_motor']} C")
    print(f"Fault:    {prog.fault_name(v['fault_code'])}")
    try:
        lay = prog.layout()
        conf = prog.read_conf(MC_ROOT)
        print(f"Mode:     {config.show(lay, MC_ROOT, 'motor_type', conf['motor_type'])}")
    except SafetyRefusal as e:
        print(f"Config:   disabled. {e}")
    return 0


def cmd_monitor(prog, args) -> int:
    cols = MONITOR_COLUMNS
    out = None
    f = open(args.log, "w", newline="") if args.log else None
    try:
        if f:
            out = csv.writer(f)
            out.writerow(cols)
        print("  ".join(c.rjust(8) for c in cols))
        t0 = time.monotonic()
        while not args.seconds or time.monotonic() - t0 < args.seconds:
            v = prog.values()
            row = prog.monitor_row(v, time.monotonic() - t0)
            print("  ".join(str(c).rjust(8) for c in row))
            for text in prog.link.collect_prints(args.period):
                print(f"[board] {text}")
            if out:
                out.writerow(row)
                f.flush()
    except KeyboardInterrupt:
        pass
    finally:
        if f:
            f.close()
    return 0


def cmd_get(prog, args) -> int:
    root = APP_ROOT if args.app else MC_ROOT
    conf, lay = prog.read_conf(root), prog.layout()
    picked = {k: v for k, v in conf.items() if fnmatch.fnmatch(k, args.pattern)}
    if not picked:
        raise SafetyRefusal(f"no config field matches `{args.pattern}`.")
    if args.json:
        print(json.dumps({k: config.show(lay, root, k, v) for k, v in picked.items()}, indent=1))
    else:
        for k, v in picked.items():
            print(f"{k} = {config.show(lay, root, k, v)}")
    return 0


def cmd_set(prog, args) -> int:
    changes = {}
    for a in args.changes:
        if "=" not in a:
            raise SafetyRefusal(f"`{a}` is not field=value.")
        k, v = a.split("=", 1)
        changes[k.strip()] = v
    prog.require_idle(prog.read_conf(MC_ROOT))
    plan = prog.plan_set(changes, accept_drift=args.accept_drift, allow_default_foc=args.allow_default_foc_params,
                         allow_risky=args.allow_risky)
    if not plan.diff:
        print("Already set: nothing to write, so flash is left alone.")
        return 0
    return _commit(prog, plan, args)


def cmd_backup(prog, args) -> int:
    drift = prog.baseline_drift()
    if drift and not args.accept_drift:
        raise SafetyRefusal(
            f"{', '.join(sorted(drift))} differ from this board's last backup, and a new backup would make them its "
            "settings, so the next write would store a script's temporary changes. Power-cycle to clear those, or "
            "pass --accept-drift if you changed them on purpose.")
    print(f"Backup saved: {prog.save_backup()}")
    return 0


def cmd_backups(prog, args) -> int:
    paths = backup.list_backups(args.backup_dir, prog.identify()["uuid"])
    print("\n".join(str(x) for x in paths) if paths else "No backups for this board yet.")
    return 0


def cmd_diff(prog, args) -> int:
    diff = prog.compare_backup(backup.load(args.file))
    lay = prog.layout()
    if not diff:
        print("The board matches this backup.")
    for k, (a, b) in sorted(diff.items()):
        print(f"  {k}: backup {config.show(lay, MC_ROOT, k, a)}, board {config.show(lay, MC_ROOT, k, b)}")
    return 0


def cmd_restore(prog, args) -> int:
    doc = backup.load(args.file)
    prog.require_idle(prog.read_conf(MC_ROOT))
    plan = prog.plan_restore(doc, allow_other_board=args.allow_other_board,
                             allow_default_foc=args.allow_default_foc_params, allow_risky=args.allow_risky)
    if not plan.diff:
        print("The board already matches this backup, so nothing was written.")
        return 0
    return _commit(prog, plan, args)


def cmd_stop(prog, args) -> int:
    prog.check_can_stop(force=args.force)
    paused = prog.stop()
    print("Script paused, motor released." if paused else "No script on the board, motor released.")
    print("Note: C libraries' own threads keep running. This does not stop the engine.")
    return 0


def cmd_start(prog, args) -> int:
    prog.check_can_start()
    _confirm(args, "This runs the script stored on the board, which may move the motor or servo. Type YES: ")
    print("Script started." if prog.start() else "Could not start: is a script stored on the board?")
    return 0


def _ask_yes(prompt: str) -> bool:
    """A typed YES at a terminal. Never yes when nobody is there to type it."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip() == "YES"
    except EOFError:
        return False


def _read_script(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SafetyRefusal(f"can't read {path}: {e}") from e


def _store_and_maybe_start(prog, args, content: bytes, what: str) -> int:
    print(f"Storing {what}: {len(content)} bytes. The board runs it at every power-up from now on.")
    print("The running script stops, and every erase and write releases the motor for up to 5 s.")
    _confirm(args)
    res = code.store(prog, content)
    print(f"Stored and read back: all {res['bytes']} bytes match.")
    print(f"Previous script backed up: {res['code_backup']}")
    print(f"Config backed up: {res['config_backup']}")
    if args.start or _ask_yes("Start it now? It may move the motor or servo. Type YES to start: "):
        prog.check_can_start()
        print("Script started." if prog.start() else "The board didn't start it. Check with `sky code backup`.")
    else:
        print("The new script is stored but not running. Run `sky start` or power-cycle to start it.")
    return 0


def cmd_code_backup(prog, args) -> int:
    content = code.read_code(prog)
    path = code.save_backup(prog, content, "manual")
    if content:
        print(f"Script backup saved ({len(content)} bytes, {code.import_count(content)} imported files): {path}")
    else:
        print(f"No valid script is stored on the board. Saved an empty backup: {path}")
    return 0


def cmd_code_backups(prog, args) -> int:
    paths = code.list_backups(args.backup_dir, prog.identify()["uuid"])
    print("\n".join(str(x) for x in paths) if paths else "No script backups for this board yet.")
    return 0


def cmd_code_try(prog, args) -> int:
    text = _read_script(args.file)
    for warning in code.check_script(text):
        print(f"Warning: {warning}")
    print(f"This runs {args.file} from RAM in place of the stored script, until the board is power-cycled.")
    _confirm(args, "It may move the motor or servo. Engine stopped, propeller clear? Type YES to run it: ")
    res = code.try_script(prog, text)
    print(f"Config backed up: {res['config_backup']}")
    print(f"Running from RAM ({res['bytes']} bytes). The stored script is unchanged. Its messages, and any error in "
          "the script, show in `sky monitor`.")  # fmt: skip
    print("Power-cycle the board, or run `sky start`, to go back to the stored script.")
    return 0


def _print_builds(builds) -> dict:
    """Report each import's build. Returns {import path: freshly built bytes}."""
    fresh = {}
    for path, name, result in builds:
        if result is None:
            print(f"{name}: {path} has no source and Makefile next to it, so there's nothing to build.")
            continue
        fresh[path] = result.data
        commit = result.commit[:8] if result.commit else "an unknown commit"
        print(f"{name}: built from source, {len(result.data)} bytes, SHA-256 {result.sha256}, gcc {result.version}, "
              f"interface files at {commit}.")  # fmt: skip
        if result.matches_committed is False:
            print(f"  The committed {path} differs from this build of its source.")
        elif result.matches_committed:
            print(f"  The committed {path} matches this build byte for byte.")
        for warning in result.warnings:
            print(f"  Warning: {warning}")
    return fresh


def cmd_code_build(args, serial_factory, ports) -> int:
    """Build and check a script's native libraries. Needs no board."""
    text = _read_script(args.file)
    code.check_script(text, native=True)
    print("Building runs each library's own Makefile, in a temporary copy: only build projects you trust.")
    builds = native.build_imports(text, Path(args.file).parent)
    if not builds:
        print("This script imports nothing, so there's nothing to build.")
        return 0
    _print_builds(builds)
    print("Nothing was sent to a board. The interface files weren't compared with a board's firmware: "
          "`sky code store --build` does that before storing.")  # fmt: skip
    return 0


def cmd_code_store(prog, args) -> int:
    text = _read_script(args.file)
    for warning in code.check_script(text, native=True):
        print(f"Warning: {warning}")
    built = None
    if args.build:
        print("Building runs each library's own Makefile, in a temporary copy: only build projects you trust.")
        builds = native.build_imports(text, Path(args.file).parent)
        built = _print_builds(builds)
        prebuilt = [path for path, _, result in builds if result is None]
        if prebuilt:
            raise SafetyRefusal(f"--build was asked for, but {', '.join(prebuilt)} has no source to build from. Store "
                                "without --build to use the file as it is.")  # fmt: skip
        firmware = native.board_commit(prog)
        for _, _, result in builds:
            native.require_matching_interface(result.commit, firmware)
    content, files = code.pack_script(text, Path(args.file).parent, built=built)
    if code.uses_native(text):
        names = ", ".join(f"{name} ({size} bytes)" for name, size in files) or "none imported"
        print(f"Native libraries: {names}.")
        for line in code.library_lines(code.unpack_imports(content)):
            print(f"  {line}")
        print(code.NATIVE_RISK)
        print(code.GUARD_NOTE)
        if not args.native_ok and not _ask_yes("Type YES to store a script with native libraries: "):
            raise SafetyRefusal("cancelled. Nothing was written.")
    return _store_and_maybe_start(prog, args, content, args.file)


def cmd_code_restore(prog, args) -> int:
    doc = code.load_backup(args.file)
    imports = code.check_restore(doc, prog.identify())
    if imports:
        print(f"This backup includes {imports} imported files, such as a native library. {code.NATIVE_RISK}")
        if not code.has_guard(code.script_text(doc["content"])):
            print("This backup has no start-up guard, so `sky code rescue` can't help if its library crashes. "
                  "Storing the script from its file with `sky code store` adds one.")  # fmt: skip
        if not args.native_ok and not _ask_yes("Type YES to restore it anyway: "):
            raise SafetyRefusal("cancelled. Nothing was written.")
    return _store_and_maybe_start(prog, args, doc["content"], f"the backup from {doc['created']}")


def cmd_code_rescue(args, serial_factory, ports) -> int:
    """Runs without a board connection: it makes its own each time the board appears."""
    print("This waits for the board to appear, then erases its stored script, so it starts with none. Your script "
          "backups stay on this computer: `sky code backups`.")  # fmt: skip
    print(code.GUARD_NOTE)
    _confirm(args, "Engine stopped, propeller clear? Type YES to erase the stored script: ")

    def find_ports():
        return ([args.port] if args.port in ports() or os.path.exists(args.port) else []) if args.port else ports()

    print(f"Waiting up to {args.wait:g} s. A boot-looping board is caught at its next power-up.")
    res = code.rescue(lambda port: Link(serial_factory(port)), find_ports, args.backup_dir, uuid=args.uuid,
                      wait_s=args.wait)  # fmt: skip
    print(f"Erased. {res['hw']}, UUID {res['uuid']}, now starts with no script. Fix the library, then store the "
          "script again.")  # fmt: skip
    return 0


def cmd_term(prog, args) -> int:
    for text in prog.terminal(args.term_command):
        print(text)
    return 0


def cmd_repl(prog, args) -> int:
    resume_note = "Note: evaluating an expression also resumes a script paused by `sky stop` or `sky set`."
    if args.expr:
        if not args.expr.strip().startswith(":"):
            print(resume_note, file=sys.stderr)
        for text in prog.repl(args.expr, allow_motor=args.allow_motor) or ["(no reply)"]:
            print(text)
        return 0
    _banner()
    print("LispBM REPL. Words that write flash, reach other boards or move the motor are blocked. Ctrl-D to leave.")
    print("Functions your own script defines aren't checked.")
    print(resume_note)
    while True:
        try:
            line = input("sky> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line.strip():
            continue
        try:
            for text in prog.repl(line, allow_motor=args.allow_motor) or ["(no reply)"]:
                print(text)
        except SafetyRefusal as e:
            print(f"Refused: {e}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sky", description=f"{APP_NAME}: read and configure VESC motor controllers.")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    ap.add_argument("--port", default=os.environ.get("SKY_PORT"), help="serial port, default: the only board found")
    ap.add_argument("--backup-dir", default=None, help=f"default: {backup.default_dir()}")
    sub = ap.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        return sp

    def add_write_flags(sp):
        sp.add_argument("--engine-stopped", action="store_true", help="state the engine is stopped; skips the prompt")
        sp.add_argument("--dry-run", action="store_true", help="show the planned changes and write nothing")
        sp.add_argument("--allow-default-foc-params", action="store_true",
                        help="allow FOC with parameters that were never measured on this motor")
        sp.add_argument("--allow-risky", action="store_true",
                        help="allow changes that remove protection or go above the board's rating")

    add("status", cmd_status, "board, firmware, live values, faults")
    sp = add("monitor", cmd_monitor, "live values and board messages")
    sp.add_argument("--log", help="also save to this CSV file")
    sp.add_argument("--seconds", type=float, default=0, help="stop after this long, default: until Ctrl-C")
    sp.add_argument("--period", type=float, default=0.1, help="seconds between samples")
    sp = add("get", cmd_get, "read config fields")
    sp.add_argument("pattern", nargs="?", default="*", help="field name or wildcard, e.g. 'foc_*'")
    sp.add_argument("--app", action="store_true", help="read the app config instead of the motor config")
    sp.add_argument("--json", action="store_true")
    sp = add("set", cmd_set, "change motor config fields, e.g. sky set motor_type=FOC")
    sp.add_argument("changes", nargs="+", metavar="field=value")
    sp.add_argument("--accept-drift", action="store_true", help="also store differences from the last backup")
    add_write_flags(sp)
    sp = add("backup", cmd_backup, "save the motor and app config to a file")
    sp.add_argument("--accept-drift", action="store_true", help="also accept differences from the last backup")
    add("backups", cmd_backups, "list this board's backups")
    sp = add("diff", cmd_diff, "compare the board's motor config with a backup")
    sp.add_argument("file")
    sp = add("restore", cmd_restore, "write a backup's motor config back to the board")
    sp.add_argument("file")
    sp.add_argument("--allow-other-board", action="store_true", help="allow a backup from another board of this type")
    add_write_flags(sp)
    sp = add("stop", cmd_stop, "pause the script and release the motor")
    sp.add_argument("--force", action="store_true", help="do it even though the motor is turning")
    sp = add("start", cmd_start, "run the script stored on the board")
    sp.add_argument("--engine-stopped", action="store_true", help="state the engine is stopped; skips the prompt")
    sp = add("term", cmd_term, "run a read-only firmware terminal command, e.g. sky term faults")
    sp.add_argument("term_command", metavar="command")
    sp = add("code", None, "back up, try and store the board's Lisp script")
    code_sub = sp.add_subparsers(dest="code_command", required=True)

    def add_code(name, func, help_text):
        csp = code_sub.add_parser(name, help=help_text, description=help_text)
        csp.set_defaults(func=func)
        return csp

    add_code("backup", cmd_code_backup, "save the stored script to a file")
    add_code("backups", cmd_code_backups, "list this board's script backups")
    csp = add_code("try", cmd_code_try, "run a script from RAM without storing it, until the next power-up")
    csp.add_argument("file")
    csp.add_argument("--engine-stopped", action="store_true", help="state the engine is stopped; skips the prompt")
    store_commands = (("store", cmd_code_store, "store a script on the board, after backing up the old one"),
                      ("restore", cmd_code_restore, "store one of this board's script backups again"))
    for name, func, help_text in store_commands:
        csp = add_code(name, func, help_text)
        csp.add_argument("file")
        csp.add_argument("--engine-stopped", action="store_true", help="state the engine is stopped; skips the prompt")
        csp.add_argument("--start", action="store_true", help="start the stored script afterwards without asking")
        csp.add_argument("--native-ok", action="store_true",
                         help="store a script or backup with native libraries without asking")  # fmt: skip
        if name == "store":
            csp.add_argument("--build", action="store_true",
                             help="build its native libraries from source first and store those builds")  # fmt: skip
    csp = add_code("build", cmd_code_build, "build and check a script's native libraries from source; writes nothing")
    csp.add_argument("file")
    csp.set_defaults(no_link=True)
    csp = add_code("rescue", cmd_code_rescue, "erase the stored script as the board powers up, to break a boot loop")
    csp.add_argument("--engine-stopped", action="store_true", help="state the engine is stopped; skips the prompt")
    csp.add_argument("--wait", type=float, default=code.RESCUE_WAIT_S, help="seconds to wait for the board")
    csp.add_argument("--uuid", help="only erase the board with this UUID (see `sky status`)")
    csp.set_defaults(no_link=True)
    sp = add("repl", cmd_repl, "LispBM console")
    sp.add_argument("-e", "--expr", help="evaluate one expression and exit")
    sp.add_argument("--allow-motor", action="store_true", help="allow words that move the motor or change limits")
    return ap


def _run(job) -> int:
    try:
        return job()
    except SafetyRefusal as e:
        print(f"Refused: {e}", file=sys.stderr)
        return e.exit_code
    except SkyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return e.exit_code


def main(argv=None, *, serial_factory=open_serial, ports=list_ports) -> int:
    args = build_parser().parse_args(argv)
    args.backup_dir = args.backup_dir or str(backup.default_dir())
    if getattr(args, "no_link", False):
        return _run(lambda: args.func(args, serial_factory, ports))
    try:
        link = Link(serial_factory(args.port or find_port()))
    except SkyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return e.exit_code
    try:
        return _run(lambda: args.func(Programmer(link, args.backup_dir), args))
    finally:
        link.close()
