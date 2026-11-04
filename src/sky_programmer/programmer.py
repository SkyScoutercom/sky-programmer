"""What the app does with a board. All config writes go through commit()."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import backup, config, safety
from . import protocol as p
from .config import APP_ROOT, MC_ROOT
from .errors import CommsError, SafetyRefusal, VerifyFailed

IDLE_MAX_ERPM = 200
IDLE_MAX_CURRENT_A = 1.0
IDLE_MAX_DUTY = 0.02
USB_ONLY_BELOW_V = 2.0  # below this the bus isn't powered, so the voltage says nothing
WRITE_TIMEOUT_S = 8.0  # the firmware may wait 3 s for motor release, then writes flash
PAUSE_TIMEOUT_S = 4.0
PAUSE_FAILED_AFTER_S = 1.5  # the firmware answers "no" at once if no script exists, after 2 s if the pause failed
SETTLE_S = 0.5
READBACK_TRIES = 5

MONITOR_COLUMNS = ("t", "erpm", "duty", "motor_a", "input_a", "bus_v", "fet_c", "motor_c", "fault")


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
        # What this app last did to the script. The board can't be asked: COMM_LISP_GET_STATS resumes a paused script.
        self.script_paused = False

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

    def fault_name(self, code: int) -> str:
        try:
            return self.layout().enums["mc_fault_code"].get(str(code), str(code))
        except SafetyRefusal:
            return str(code)

    def monitor_row(self, v: dict, elapsed: float) -> list:
        """One row of values, in MONITOR_COLUMNS order."""
        return [f"{elapsed:.1f}", v["erpm"], v["duty"], v["current_motor"], v["current_in"], v["v_in"], v["temp_fet"],
                v["temp_motor"], self.fault_name(v["fault_code"])]

    def _read_blob(self, command: int) -> bytes:
        return self.link.request(bytes([command]))[1:]

    def read_mc_stable(self) -> bytes:
        """Two reads must agree, so a config that is changing right now is never used as a base."""
        lay = self.layout()
        a, b = self._read_blob(p.COMM_GET_MCCONF), self._read_blob(p.COMM_GET_MCCONF)
        if a != b:
            raise SafetyRefusal("the motor config changed between two reads, so something is modifying it. "
                                "Usually a running script: run `sky stop` first.")
        config.decode(a, lay, MC_ROOT)
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

    def _check_baseline(self, before: dict) -> None:
        """A write stores the whole live config, including temporary changes made by a script."""
        ident, lay = self.identify(), self.layout()
        base = backup.latest(self.backup_dir, ident["uuid"], ident["fw"])
        if base is None:
            raise SafetyRefusal(
                "this board has no SKY backup yet, so temporary changes made by a script can't be told apart from "
                "its settings, and a write would store them. Run `sky backup` first, ideally just after power-up "
                "with the engine stopped, or pass --accept-drift.")
        drift = config.differences(config.decode(base["mc_blob"], lay, MC_ROOT), before, lay)
        if drift:
            lines = "\n".join(f"  {k}: {config.show(lay, MC_ROOT, k, a)} -> {config.show(lay, MC_ROOT, k, b)}"
                              for k, (a, b) in sorted(drift.items()))
            raise SafetyRefusal(
                "the live config differs from this board's last SKY backup:\n" + lines + "\n"
                "A write stores all of it, including temporary changes made by a script. Power-cycle to clear those, "
                "or pass --accept-drift to store them.")

    # ---------------------------------------------------------------------- planning
    def plan_set(self, changes: dict, *, accept_drift=False, allow_default_foc=False, allow_risky=False) -> Plan:
        """Build and check a write of field=value text. Sends nothing except reads."""
        lay = self.layout()
        parsed = {f: config.parse_value(lay, MC_ROOT, f, t) for f, t in changes.items()}
        current = self.read_mc_stable()
        target = config.patch_blob(current, lay, MC_ROOT, parsed)
        return self._plan(current, target, set(parsed), accept_drift, allow_default_foc, allow_risky)

    def plan_restore(self, doc: dict, *, allow_other_board=False, allow_default_foc=False, allow_risky=False) -> Plan:
        ident, lay = self.identify(), self.layout()
        if doc["fw"] != ident["fw"]:
            raise SafetyRefusal(f"this backup is from firmware {doc['fw']}, the board runs {ident['fw']}.")
        if doc["hw"] != ident["hw"]:
            raise SafetyRefusal(f"this backup is from hardware `{doc['hw']}`, the board is `{ident['hw']}`.")
        if doc["uuid"] != ident["uuid"] and not allow_other_board:
            raise SafetyRefusal("this backup is from another board of the same type. "
                                "Use --allow-other-board if you mean it.")
        current = self.read_mc_stable()
        before = config.decode(current, lay, MC_ROOT)
        wanted = config.differences(before, config.decode(doc["mc_blob"], lay, MC_ROOT), lay)
        # Patched onto the live config, so this board's own calibration offsets are always kept.
        target = config.patch_blob(current, lay, MC_ROOT, {k: v[1] for k, v in wanted.items()})
        # A restore names every field, so differing from the latest backup is the point.
        return self._plan(current, target, set(wanted), True, allow_default_foc, allow_risky)

    def _plan(self, current, target, requested, accept_drift, allow_default_foc, allow_risky) -> Plan:
        lay = self.layout()
        before = config.decode(current, lay, MC_ROOT)
        after = config.decode(target, lay, MC_ROOT)
        diff = config.differences(before, after)
        plan = Plan(current, target, requested, before, after, diff)
        if not diff:
            return plan
        unexpected = set(diff) - requested
        if unexpected:
            raise SafetyRefusal(f"internal check failed: {sorted(unexpected)} would also change. Not writing.")
        new_bad = config.motor_rule_violations(after, lay) - config.motor_rule_violations(before, lay)
        problems = sorted(new_bad) + config.range_problems(diff, after, lay)
        if problems:
            raise SafetyRefusal("this config would be invalid:\n  - " + "\n  - ".join(problems))
        risky = config.risky_changes(diff, before, after, lay, self.identify()["hw"])
        if risky and not allow_risky:
            raise SafetyRefusal("this change can damage hardware or remove protection:\n  - " + "\n  - ".join(risky)
                                + "\nPass --allow-risky if you mean it.")
        self._check_supply_stays_in_range(after)
        if "motor_type" in diff:
            self._check_motor_type_change(plan, allow_default_foc)
        if not accept_drift:
            self._check_baseline(before)
        return plan

    def _check_supply_stays_in_range(self, after: dict) -> None:
        """A range excluding the present supply would lock out later writes."""
        v = self.values()["v_in"]
        if v >= USB_ONLY_BELOW_V and not after["l_min_vin"] <= v <= after["l_max_vin"]:
            raise SafetyRefusal(f"the present bus voltage, {v} V, would be outside the new "
                                f"{after['l_min_vin']}-{after['l_max_vin']} V range.")

    def _check_motor_type_change(self, plan: Plan, allow_default_foc: bool) -> None:
        lay = self.layout()
        new = lay.enum_of(MC_ROOT, "motor_type").get(str(plan.after["motor_type"]))
        if new != "MOTOR_TYPE_FOC":
            return
        defaults = config.decode(self._read_blob(p.COMM_GET_MCCONF_DEFAULT), lay, MC_ROOT)
        params = ("foc_motor_r", "foc_motor_l", "foc_motor_flux_linkage")
        unmeasured = [f for f in params if plan.after[f] == defaults[f]]
        if unmeasured and not allow_default_foc:
            raise SafetyRefusal(
                f"{', '.join(unmeasured)} still at the firmware default, so they don't describe this motor. Set the "
                "measured values in the same command, or pass --allow-default-foc-params.")
        plan.warnings.append("FOC start-up calibration drives the phases at about 50 % duty. "
                             "Keep the engine stopped whenever the board powers up in FOC.")
        plan.warnings.append("foc_current_kp, foc_current_ki and the observer gain don't follow the motor values "
                             "automatically. Check them too.")

    # ---------------------------------------------------------------------- the write path
    def commit(self, plan: Plan, reason: str = "pre-write") -> dict:
        """Check idle, pause the script, save a backup, write once, read back, roll back if the result is wrong.

        A write is never retried: if the reply is lost, the read-back decides what happened.
        """
        lay, ident = self.layout(), self.identify()
        self.require_idle(plan.before)  # first, so a turning engine never loses its script to this command
        self._pause_script()
        try:
            self.require_idle(plan.before)
            if self.read_mc_stable() != plan.current:
                raise SafetyRefusal("the motor config changed since this write was planned.")
            app_blob = self._read_blob(p.COMM_GET_APPCONF)
            saved = backup.save(self.backup_dir, ident, reason, plan.current, app_blob, plan.before)
        except SafetyRefusal as e:
            raise SafetyRefusal(f"{e} Nothing was written. The script is now paused: run `sky start` "
                                "or power-cycle to run it again.") from e
        self.say(f"Backup saved: {saved}")

        acked = self._send_config(plan.target)
        got = self._read_back()
        try:
            after = config.decode(got, lay, MC_ROOT)
        except SafetyRefusal as e:
            raise VerifyFailed(f"the read-back after the write could not be used: {e} Backup: {saved}") from e
        off = config.differences(plan.after, after, lay)

        if not off:
            self._post_backup(app_blob, got, after)
            return {"backup": str(saved), "acked": acked, "limited": {}}
        if not config.differences(plan.before, after, lay):
            raise VerifyFailed(f"the board did not take the write and still holds the old config. Backup: {saved}")

        unexpected = {k: v for k, v in off.items() if k not in plan.requested and k not in config.FIRMWARE_ADJUSTED}
        if unexpected:
            self.say(f"Fields that should not have changed did: {sorted(unexpected)}. Rolling back.")
            self._rollback(plan, saved)
            raise VerifyFailed(f"the write changed {sorted(unexpected)}, so it was rolled back. Backup: {saved}")
        # Only requested or firmware-managed fields differ: the firmware applied its own limits.
        self._post_backup(app_blob, got, after)
        return {"backup": str(saved), "acked": acked, "limited": dict(off)}

    def _pause_script(self) -> bool:
        """True if a script was paused, False if none exists. Refuses if one may still be running."""
        t0 = self.link.clock()
        reply = self.link.request(bytes([p.COMM_LISP_SET_RUNNING, 0]), timeout=PAUSE_TIMEOUT_S)
        if len(reply) > 1 and reply[1] == 1:
            self.script_paused = True
            return True
        if self.link.clock() - t0 >= PAUSE_FAILED_AFTER_S:
            raise SafetyRefusal("the script did not pause within 2 s, so it may still be running. "
                                "Nothing was written. Power-cycle with the engine stopped and try again.")
        return False

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
        if config.differences(plan.before, back, self.layout()):
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
        lay = self.layout()
        blob = self.read_mc_stable()
        app = self._read_blob(p.COMM_GET_APPCONF)
        config.decode(app, lay, APP_ROOT)
        return backup.save(self.backup_dir, self.identify(), reason, blob, app, config.decode(blob, lay, MC_ROOT))

    def compare_backup(self, doc: dict) -> dict:
        """{field: (backup value, board value)} for motor config fields that differ."""
        ident = self.identify()
        if doc["fw"] != ident["fw"]:
            raise SafetyRefusal(f"this backup is from firmware {doc['fw']}, the board runs {ident['fw']}.")
        saved = config.decode(doc["mc_blob"], self.layout(), MC_ROOT)
        return config.differences(saved, self.read_conf(MC_ROOT))

    def check_can_stop(self, force: bool = False) -> None:
        """Pausing takes away the script's throttle and kill control, so a turning motor refuses it."""
        erpm = self.values()["erpm"]
        if abs(erpm) > IDLE_MAX_ERPM and not force:
            raise SafetyRefusal(
                f"the motor is turning ({erpm} erpm), so the engine may be running. Pausing the script takes away "
                "its throttle and kill control and leaves the servo where it is. Kill the engine with the RC switch. "
                "--force overrides this.")

    def check_can_start(self) -> None:
        erpm = self.values()["erpm"]
        if abs(erpm) > IDLE_MAX_ERPM:
            raise SafetyRefusal(f"the motor is turning ({erpm} erpm). Stop it before restarting the script.")

    def stop(self) -> bool:
        """Pause the script and release the motor, in that order. Returns whether a script was paused."""
        try:
            return self._pause_script()
        finally:
            self.link.send(safety.MOTOR_RELEASE_PACKET)

    def start(self) -> bool:
        """Restart the script stored on the board. Returns whether it started."""
        ok = self.link.request(bytes([p.COMM_LISP_SET_RUNNING, 1]), timeout=5.0)
        started = len(ok) > 1 and ok[1] == 1
        if started:
            self.script_paused = False
        return started

    def terminal(self, command: str, wait: float = 0.5) -> list[str]:
        self.link.send(bytes([p.COMM_TERMINAL_CMD]) + command.encode() + b"\0")
        return self.link.collect_prints(wait)

    def repl(self, text: str, *, allow_motor=False, wait: float = 0.6) -> list[str]:
        self.layout()  # the word lists are only reviewed for supported firmware
        self.link.send(bytes([p.COMM_LISP_REPL_CMD]) + text.encode() + b"\0", repl_allow_motor=allow_motor)
        if not text.strip().startswith(":"):
            self.script_paused = False  # the firmware resumes the evaluator for any expression
        return self.link.collect_prints(wait)
