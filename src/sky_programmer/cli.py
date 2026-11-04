"""The `sky` command."""
from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import sys
import time

from . import APP_NAME, __version__, backup, config
from .config import APP_ROOT, MC_ROOT
from .errors import SafetyRefusal, SkyError
from .link import Link, find_port, open_serial
from .programmer import IDLE_MAX_ERPM, Programmer

ENGINE_PROMPT = "Engine stopped, propeller clear, supply stable? Type YES to write: "


def _confirm(args, prompt=ENGINE_PROMPT) -> None:
    """The operator states the engine is stopped: the board can't see it behind the starter relay."""
    if args.engine_stopped:
        return
    if not sys.stdin.isatty():
        raise SafetyRefusal("this command needs --engine-stopped when it is not run interactively.")
    if input(prompt).strip() != "YES":
        raise SafetyRefusal("cancelled. Nothing was written.")


def _print_plan(prog, plan) -> None:
    lay = prog.layout()
    print("Planned changes:")
    for k, (a, b) in sorted(plan.diff.items()):
        print(f"  {k}: {config.show(lay, MC_ROOT, k, a)} -> {config.show(lay, MC_ROOT, k, b)}")
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
    print("Written and verified by read-back.")
    print("The script is paused. Run `sky start` or power-cycle to run it again.")
    return 0


def cmd_status(prog, args) -> int:
    ident = prog.identify()
    print(f"Board:    {ident['hw']}")
    print(f"Firmware: {ident['fw']}" + (f" (test build {ident['test_version']})" if ident["test_version"] else ""))
    print(f"UUID:     {ident['uuid']}")
    v = prog.values()
    print(f"Bus:      {v['v_in']} V    Motor: {v['erpm']} erpm, {v['current_motor']} A, duty {v['duty']}")
    print(f"Temps:    MOSFET {v['temp_fet']} C, motor {v['temp_motor']} C")
    print(f"Fault:    {prog.fault_name(v['fault_code'])}")
    s = prog.lisp_stats()
    print(f"Script:   CPU {s['cpu']} %, heap {s['heap']} %, memory {s['mem']} %")
    try:
        lay = prog.layout()
        conf = prog.read_conf(MC_ROOT)
        print(f"Mode:     {config.show(lay, MC_ROOT, 'motor_type', conf['motor_type'])}")
    except SafetyRefusal as e:
        print(f"Config:   disabled. {e}")
    return 0


def cmd_monitor(prog, args) -> int:
    cols = ["t", "erpm", "duty", "motor_a", "input_a", "bus_v", "fet_c", "motor_c", "fault"]
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
            row = [f"{time.monotonic() - t0:.1f}", v["erpm"], v["duty"], v["current_motor"], v["current_in"],
                   v["v_in"], v["temp_fet"], v["temp_motor"], prog.fault_name(v["fault_code"])]
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
    plan = prog.plan_set(changes, accept_drift=args.accept_drift, allow_default_foc=args.allow_default_foc_params)
    if not plan.diff:
        print("Already set: nothing to write, so flash is left alone.")
        return 0
    return _commit(prog, plan, args)


def cmd_backup(prog, args) -> int:
    print(f"Backup saved: {prog.save_backup()}")
    return 0


def cmd_backups(prog, args) -> int:
    paths = backup.list_backups(args.backup_dir, prog.identify()["uuid"])
    print("\n".join(str(x) for x in paths) if paths else "No backups for this board yet.")
    return 0


def cmd_diff(prog, args) -> int:
    doc = backup.load(args.file)
    ident = prog.identify()
    if doc["fw"] != ident["fw"]:
        raise SafetyRefusal(f"this backup is from firmware {doc['fw']}, the board runs {ident['fw']}.")
    lay = prog.layout()
    saved = config.decode(doc["mc_blob"], lay, MC_ROOT)
    diff = config.differences(saved, prog.read_conf(MC_ROOT))
    if not diff:
        print("The board matches this backup.")
    for k, (a, b) in sorted(diff.items()):
        print(f"  {k}: backup {config.show(lay, MC_ROOT, k, a)}, board {config.show(lay, MC_ROOT, k, b)}")
    return 0


def cmd_restore(prog, args) -> int:
    doc = backup.load(args.file)
    prog.require_idle(prog.read_conf(MC_ROOT))
    plan = prog.plan_restore(doc, allow_other_board=args.allow_other_board,
                             allow_default_foc=args.allow_default_foc_params)
    if not plan.diff:
        print("The board already matches this backup, so nothing was written.")
        return 0
    return _commit(prog, plan, args)


def cmd_stop(prog, args) -> int:
    v = prog.values()
    if abs(v["erpm"]) > IDLE_MAX_ERPM and not args.force:
        raise SafetyRefusal(
            f"the motor is turning ({v['erpm']} erpm), so the engine may be running. Pausing the script takes away "
            "its throttle and kill control and leaves the servo where it is. Kill the engine with the RC switch. "
            "--force overrides this.")
    paused = prog.stop()
    print("Script paused, motor released." if paused else "No script running, motor released.")
    print("Note: C libraries' own threads keep running. This does not stop the engine.")
    return 0


def cmd_start(prog, args) -> int:
    _confirm(args, "This runs the script stored on the board, which may move the motor or servo. Type YES: ")
    print("Script started." if prog.start() else "Could not start: is a script stored on the board?")
    return 0


def cmd_term(prog, args) -> int:
    for text in prog.terminal(args.term_command):
        print(text)
    return 0


def cmd_repl(prog, args) -> int:
    if args.expr:
        for text in prog.repl(args.expr, allow_motor=args.allow_motor) or ["(no reply)"]:
            print(text)
        return 0
    print("LispBM REPL. Commands that write flash are blocked. Ctrl-D to leave.")
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
    ap.add_argument("--port", default=os.environ.get("SKY_PORT"), help="serial port, default: the only /dev/ttyACM*")
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
    add("backup", cmd_backup, "save the motor and app config to a file")
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
    sp = add("repl", cmd_repl, "LispBM console")
    sp.add_argument("-e", "--expr", help="evaluate one expression and exit")
    sp.add_argument("--allow-motor", action="store_true", help="allow words that move the motor or change limits")
    return ap


def main(argv=None, *, serial_factory=open_serial) -> int:
    args = build_parser().parse_args(argv)
    args.backup_dir = args.backup_dir or str(backup.default_dir())
    try:
        link = Link(serial_factory(args.port or find_port()))
    except SkyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return e.exit_code
    try:
        return args.func(Programmer(link, args.backup_dir), args)
    except SafetyRefusal as e:
        print(f"Refused: {e}", file=sys.stderr)
        return e.exit_code
    except SkyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return e.exit_code
    finally:
        link.close()
