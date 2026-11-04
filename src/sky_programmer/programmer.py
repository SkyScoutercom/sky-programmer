"""What the app does with a board. All config writes go through commit()."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import backup, config
from . import protocol as p
from .config import APP_ROOT, MC_ROOT
from .errors import CommsError, SafetyRefusal, VerifyFailed

IDLE_MAX_ERPM = 200
IDLE_MAX_CURRENT_A = 1.0
IDLE_MAX_DUTY = 0.02
USB_ONLY_BELOW_V = 2.0  # below this the bus isn't powered, so the voltage says nothing
WRITE_TIMEOUT_S = 8.0  # the firmware may wait 3 s for motor release, then writes flash
SETTLE_S = 0.5
READBACK_TRIES = 5


@dataclass
class Plan:
    """One proposed write, ready to show the operator."""

    current: bytes
    target: bytes
    requested: set
    before: dict
    after: dict
    diff: dict
    warnings: list = field(default_factory=list)


class Programmer:
    def __init__(self, link, backup_dir, say=print):
        self.link, self.backup_dir, self.say = link, backup_dir, say
        self._ident, self._layout = None, None

    # ---------------------------------------------------------------------- reading
    def identify(self) -> dict:
        if self._ident is None:
            self._ident = p.parse_fw_version(self.link.request(bytes([p.COMM_FW_VERSION])))
        return self._ident

    def layout(self) -> config.Layout:
        if self._layout is None:
            ident = self.identify()
            self._layout = config.load_layout(ident["major"], ident["minor"])
        return self._layout

    def values(self) -> dict:
        return p.parse_values(self.link.request(bytes([p.COMM_GET_VALUES])))

    def lisp_stats(self) -> dict:
        return p.parse_lisp_stats(self.link.request(bytes([p.COMM_LISP_GET_STATS])))

    def fault_name(self, code: int) -> str:
        try:
            return self.layout().enums["mc_fault_code"].get(str(code), str(code))
        except SafetyRefusal:
            return str(code)

    def _read_blob(self, command: int) -> bytes:
        return self.link.request(bytes([command]))[1:]

    def read_mc_stable(self) -> bytes:
        """Two reads must agree, so a config that is changing right now is never used as a base."""
        a, b = self._read_blob(p.COMM_GET_MCCONF), self._read_blob(p.COMM_GET_MCCONF)
        if a != b:
            raise SafetyRefusal("the motor config changed between two reads, so something is modifying it "
                                "(probably a running script). Run `sky stop` first.")
        config.decode(a, self.layout(), MC_ROOT)
        return a

    def read_conf(self, root: str) -> dict:
        if root == MC_ROOT:
            return config.decode(self.read_mc_stable(), self.layout(), MC_ROOT)
        return config.decode(self._read_blob(p.COMM_GET_APPCONF), self.layout(), APP_ROOT)

    # ---------------------------------------------------------------------- guards
    def require_idle(self, conf: dict) -> None:
        """Refuse unless the motor is still on two samples and the supply is in range."""
        for i in range(2):
            v = self.values()
            if abs(v["erpm"]) > IDLE_MAX_ERPM or abs(v["current_motor"]) > IDLE_MAX_CURRENT_A \
                    or abs(v["duty"]) > IDLE_MAX_DUTY:
                raise SafetyRefusal(
                    f"the motor is not idle (erpm {v['erpm']}, {v['current_motor']} A, duty {v['duty']}). "
                    "Stop the engine and motor first.")
            if v["v_in"] >= USB_ONLY_BELOW_V and not conf["l_min_vin"] <= v["v_in"] <= conf["l_max_vin"]:
                raise SafetyRefusal(
                    f"bus voltage {v['v_in']} V is outside {conf['l_min_vin']}-{conf['l_max_vin']} V. "
                    "A failing supply could interrupt the flash write.")
            if i == 0:
                self.link.sleep(0.15)
        if v["fault_code"]:
            self.say(f"Note: the board reports fault {self.fault_name(v['fault_code'])}.")

    def _drift(self, before: dict) -> dict:
        ident = self.identify()
        base = backup.latest(self.backup_dir, ident["uuid"], ident["fw"])
        if base is None:
            return {}
        return config.differences(config.decode(base["mc_blob"], self.layout(), MC_ROOT), before)

    # ---------------------------------------------------------------------- planning
    def plan_set(self, changes: dict, *, accept_drift=False, allow_default_foc=False) -> Plan:
        """Build and check a write of field=value text. Sends nothing except reads."""
        lay = self.layout()
        parsed = {f: config.parse_value(lay, MC_ROOT, f, t) for f, t in changes.items()}
        current = self.read_mc_stable()
        target = config.patch_blob(current, lay, MC_ROOT, parsed)
        return self._plan(current, target, set(parsed), accept_drift, allow_default_foc)

    def plan_restore(self, doc: dict, *, allow_other_board=False, allow_default_foc=False) -> Plan:
        ident, lay = self.identify(), self.layout()
        if doc["fw"] != ident["fw"]:
            raise SafetyRefusal(f"this backup is from firmware {doc['fw']}, the board runs {ident['fw']}.")
        if doc["hw"] != ident["hw"]:
            raise SafetyRefusal(f"this backup is from hardware `{doc['hw']}`, the board is `{ident['hw']}`.")
        if doc["uuid"] != ident["uuid"] and not allow_other_board:
            raise SafetyRefusal("this backup is from another board of the same type. "
                                "Use --allow-other-board if you mean it.")
        current = self.read_mc_stable()
        config.decode(doc["mc_blob"], lay, MC_ROOT)
        # A restore is explicit about every field, so drift from the latest backup is the point.
        before = config.decode(current, lay, MC_ROOT)
        wanted = config.differences(before, config.decode(doc["mc_blob"], lay, MC_ROOT))
        # Patched onto the live config, so this board's own calibration offsets are always kept.
        target = config.patch_blob(current, lay, MC_ROOT, {k: v[1] for k, v in wanted.items()})
        return self._plan(current, target, set(wanted), True, allow_default_foc)

    def _plan(self, current, target, requested, accept_drift, allow_default_foc) -> Plan:
        lay = self.layout()
        before = config.decode(current, lay, MC_ROOT)
        after = config.decode(target, lay, MC_ROOT)
        diff = config.differences(before, after)
        unexpected = set(diff) - requested
        if unexpected:
            raise SafetyRefusal(f"internal check failed: {sorted(unexpected)} would also change. Not writing.")
        new_bad = config.motor_rule_violations(after, lay) - config.motor_rule_violations(before, lay)
        if new_bad:
            raise SafetyRefusal("this config would be invalid:\n  - " + "\n  - ".join(sorted(new_bad)))
        plan = Plan(current, target, requested, before, after, diff)
        if not accept_drift:
            drift = self._drift(before)
            if drift:
                lines = "\n".join(f"  {k}: {config.show(lay, MC_ROOT, k, a)} -> {config.show(lay, MC_ROOT, k, b)}"
                                  for k, (a, b) in sorted(drift.items()))
                raise SafetyRefusal(
                    "the live config differs from this board's last SKY backup:\n" + lines + "\n"
                    "A write stores all of it, including temporary changes made by a script. Power-cycle with the "
                    "script stopped to clear those, or pass --accept-drift to store them.")
        if "motor_type" in diff:
            self._check_motor_type_change(plan, allow_default_foc)
        return plan

    def _check_motor_type_change(self, plan: Plan, allow_default_foc: bool) -> None:
        lay = self.layout()
        new = lay.enum_of(MC_ROOT, "motor_type").get(str(plan.after["motor_type"]))
        if new != "MOTOR_TYPE_FOC":
            return
        defaults = config.decode(self._read_blob(p.COMM_GET_MCCONF_DEFAULT), lay, MC_ROOT)
        params = ("foc_motor_r", "foc_motor_l", "foc_motor_flux_linkage")
        if not allow_default_foc and all(plan.after[f] == defaults[f] for f in params):
            raise SafetyRefusal(
                "foc_motor_r, foc_motor_l and foc_motor_flux_linkage are still the firmware defaults, so they don't "
                "describe this motor. Set the measured values in the same command, or pass "
                "--allow-default-foc-params.")
        plan.warnings.append("FOC start-up calibration drives the phases at about 50 % duty. "
                             "Keep the engine stopped whenever the board powers up in FOC.")

    # ---------------------------------------------------------------------- the write path
    def commit(self, plan: Plan, reason: str = "pre-write") -> dict:
        """Pause the script, save a backup, write once, read back, roll back if the result is wrong.

        A write is never retried: if the reply is lost, the read-back decides what happened.
        """
        lay, ident = self.layout(), self.identify()
        self._pause_script()
        self.require_idle(plan.before)
        if self.read_mc_stable() != plan.current:
            raise SafetyRefusal("the motor config changed since this write was planned. Nothing was written.")
        app_blob = self._read_blob(p.COMM_GET_APPCONF)
        saved = backup.save(self.backup_dir, ident, reason, plan.current, app_blob, plan.before)
        self.say(f"Backup saved: {saved}")

        acked = self._send_config(plan.target)
        got = self._read_back()
        after = config.decode(got, lay, MC_ROOT)
        off = config.differences(plan.after, after)

        if not off:
            self._post_backup(app_blob, got, after)
            return {"backup": str(saved), "acked": acked, "limited": {}}
        if not config.differences(plan.before, after):
            raise VerifyFailed(f"the board did not take the write and still holds the old config. Backup: {saved}")

        unexpected = {k: v for k, v in off.items() if k not in plan.requested}
        if unexpected:
            self.say(f"Fields that should not have changed did: {sorted(unexpected)}. Rolling back.")
            self._rollback(plan, saved)
            raise VerifyFailed(f"the write changed {sorted(unexpected)}, so it was rolled back. Backup: {saved}")
        # Only requested fields differ: the firmware applied its hardware limits to them.
        self._post_backup(app_blob, got, after)
        return {"backup": str(saved), "acked": acked, "limited": {k: (v[0], v[1]) for k, v in off.items()}}

    def _pause_script(self) -> None:
        # Replies False when no script thread exists; either way no script is evaluating afterwards.
        self.link.request(bytes([p.COMM_LISP_SET_RUNNING, 0]), timeout=3.0)

    def _send_config(self, blob: bytes) -> bool:
        try:
            self.link.request(bytes([p.COMM_SET_MCCONF]) + blob, timeout=WRITE_TIMEOUT_S, config_write=True)
            return True
        except CommsError:
            self.say("No reply to the write. Not retrying; reading the board to see what happened.")
            return False

    def _read_back(self) -> bytes:
        for _ in range(READBACK_TRIES):
            self.link.sleep(SETTLE_S)
            try:
                return self._read_blob(p.COMM_GET_MCCONF)
            except CommsError:
                continue
        raise CommsError("the board stopped answering after the write, so its state is unknown. Don't retry the "
                         "write. Reconnect and run `sky status`, then `sky diff <backup>`.")

    def _rollback(self, plan: Plan, saved) -> None:
        self._send_config(plan.current)
        back = config.decode(self._read_back(), self.layout(), MC_ROOT)
        if config.differences(plan.before, back):
            raise VerifyFailed(f"the rollback did not fully apply either. Run `sky diff {saved}`, "
                               f"then `sky restore {saved}` after checking the board.")

    def _post_backup(self, app_blob: bytes, blob: bytes, values: dict) -> None:
        # The new baseline for the drift check. The write itself already succeeded.
        try:
            backup.save(self.backup_dir, self.identify(), "post-write", blob, app_blob, values)
        except SafetyRefusal as e:
            self.say(f"Warning: the write is good, but no new baseline was saved: {e}")

    # ---------------------------------------------------------------------- other actions
    def save_backup(self, reason: str = "manual"):
        blob = self.read_mc_stable()
        app = self._read_blob(p.COMM_GET_APPCONF)
        config.decode(app, self.layout(), APP_ROOT)
        return backup.save(self.backup_dir, self.identify(), reason, blob, app,
                           config.decode(blob, self.layout(), MC_ROOT))

    def stop(self) -> bool:
        """Pause the script and release the motor. Returns whether a script was paused."""
        ok = self.link.request(bytes([p.COMM_LISP_SET_RUNNING, 0]), timeout=3.0)
        self.link.send(bytes([p.COMM_SET_CURRENT, 0, 0, 0, 0]))
        return len(ok) > 1 and ok[1] == 1

    def start(self) -> bool:
        """Restart the script stored on the board. Returns whether it started."""
        ok = self.link.request(bytes([p.COMM_LISP_SET_RUNNING, 1]), timeout=5.0)
        return len(ok) > 1 and ok[1] == 1

    def terminal(self, command: str, wait: float = 0.5) -> list[str]:
        self.link.send(bytes([p.COMM_TERMINAL_CMD]) + command.encode() + b"\0")
        return self.link.collect_prints(wait)

    def repl(self, text: str, *, allow_motor=False, wait: float = 0.6) -> list[str]:
        self.link.send(bytes([p.COMM_LISP_REPL_CMD]) + text.encode() + b"\0", repl_allow_motor=allow_motor)
        return self.link.collect_prints(wait)
