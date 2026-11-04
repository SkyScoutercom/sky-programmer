"""The write path, against a simulated board, including the failures that could leave it damaged."""
import json

import pytest

from fake_vesc import LAYOUT, FakeVesc
from sky_programmer import backup, config
from sky_programmer import protocol as p
from sky_programmer.config import MC_ROOT
from sky_programmer.errors import CommsError, SafetyRefusal, VerifyFailed


def write(prog, **changes):
    plan = prog.plan_set({k: str(v) for k, v in changes.items()})
    return prog.commit(plan)


def backups(prog):
    return backup.list_backups(prog.backup_dir, prog.identify()["uuid"])


# ------------------------------------------------------------------------- normal use
def test_bldc_to_foc_with_measured_values(board):
    fake, prog = board()
    write(prog, motor_type="FOC")
    assert fake.value("motor_type") == 2
    assert len(fake.written) == 1


def test_only_named_field_bytes_change(board):
    fake, prog = board()
    before = fake.mc
    write(prog, l_current_max=50)
    changed = [i for i, (a, b) in enumerate(zip(before, fake.mc, strict=True)) if a != b]
    assert changed and max(changed) - min(changed) < 4


def test_order_is_pause_then_backup_then_one_write(board):
    fake, prog = board()
    write(prog, l_current_max=50)
    assert fake.script_running is False  # checked by FakeVesc at write time too
    assert fake.commands.index(p.COMM_LISP_SET_RUNNING) < fake.commands.index(p.COMM_SET_MCCONF)
    pre = [x for x in backups(prog) if "pre-write" in x.name]
    assert len(pre) == 1
    assert backup.load(pre[0])["mc_blob"] != fake.mc


def test_script_is_not_restarted_automatically(board):
    fake, prog = board()
    write(prog, l_current_max=50)
    assert fake.script_running is False


def test_restore_returns_the_old_config(board):
    fake, prog = board()
    original = fake.mc
    write(prog, l_current_max=50)
    pre = next(x for x in backups(prog) if "pre-write" in x.name)
    prog.commit(prog.plan_restore(backup.load(pre)))
    assert fake.mc == original


def test_no_change_means_no_flash_write(board):
    fake, prog = board()
    plan = prog.plan_set({"l_current_max": "70"})
    assert not plan.diff
    assert fake.written == []


def test_planning_writes_nothing(board):
    fake, prog = board()
    prog.plan_set({"motor_type": "FOC"})
    assert fake.written == [] and fake.script_running is True
    assert p.COMM_LISP_SET_RUNNING not in fake.commands


# ------------------------------------------------------------------------- refusals before any write
@pytest.mark.parametrize("field,value,why", [
    ("si_motor_poles", "7", "even"), ("foc_motor_flux_linkage", "-2", "negative"), ("l_current_max", "0", "above 0"),
    ("l_abs_current_max", "10", "below"), ("l_min_vin", "90", "below l_max_vin"), ("m_bldc_f_sw_min", "50000", "above"),
    ("l_battery_cut_end", "45", "above"), ("l_max_duty", "1.5", "at most 1"),
])  # fmt: skip
def test_bad_values_are_refused(board, field, value, why):
    fake, prog = board()
    with pytest.raises(SafetyRefusal, match=why):
        prog.plan_set({field: value})
    assert fake.written == []


@pytest.mark.parametrize("field,value", [
    ("no_such_field", "1"), ("_signature", "1"), ("motor_type", "WARP"), ("motor_type", "99"),
    ("l_current_max", "abc"), ("l_current_max", "nan"), ("l_current_max", "inf"), ("foc_offsets_current[0]", "1"),
    ("si_motor_poles", "1.5"), ("si_motor_poles", "300"), ("m_invert_direction", "maybe"),
])  # fmt: skip
def test_bad_input_is_refused(board, field, value):
    fake, prog = board()
    with pytest.raises(SafetyRefusal):
        prog.commit(prog.plan_set({field: value}))
    assert fake.written == []


def test_foc_refused_with_never_measured_parameters(board):
    fake, prog = board(values={"foc_motor_r": 0.015, "foc_motor_l": 7.5e-6, "foc_motor_flux_linkage": 0.00245})
    with pytest.raises(SafetyRefusal, match="firmware defaults"):
        prog.plan_set({"motor_type": "FOC"})
    assert fake.written == []


def test_foc_accepted_when_measured_values_come_with_it(board):
    fake, prog = board(values={"foc_motor_r": 0.015, "foc_motor_l": 7.5e-6, "foc_motor_flux_linkage": 0.00245})
    plan = prog.plan_set({"motor_type": "FOC", "foc_motor_r": "0.0081", "foc_motor_l": "4.97e-6",
                          "foc_motor_flux_linkage": "0.0018"})  # fmt: skip
    assert any("calibration" in w for w in plan.warnings)
    prog.commit(plan)
    assert fake.value("motor_type") == 2


@pytest.mark.parametrize("attr,val", [("erpm", 5000), ("erpm", -5000), ("current", 3.0), ("duty", 0.2)])
def test_refused_while_motor_is_moving(board, attr, val):
    fake, prog = board()
    plan = prog.plan_set({"l_current_max": "50"})
    setattr(fake, attr, val)
    with pytest.raises(SafetyRefusal, match="not idle"):
        prog.commit(plan)
    assert fake.written == []


def test_refused_if_pausing_the_script_reveals_a_spinning_motor(board):
    fake, prog = board(spin_after_pause=3000)
    plan = prog.plan_set({"l_current_max": "50"})
    with pytest.raises(SafetyRefusal, match="not idle"):
        prog.commit(plan)
    assert fake.written == []


@pytest.mark.parametrize("volts", [6.0, 75.0])
def test_refused_when_supply_is_out_of_range(board, volts):
    fake, prog = board()
    plan = prog.plan_set({"l_current_max": "50"})
    fake.v_in = volts
    with pytest.raises(SafetyRefusal, match="outside"):
        prog.commit(plan)
    assert fake.written == []


def test_usb_only_power_is_allowed(board):
    fake, prog = board()
    fake.v_in = 0.3
    write(prog, l_current_max=50)
    assert fake.value("l_current_max") == 50


@pytest.mark.parametrize("fw", [(6, 6), (5, 2), (7, 1)])
def test_unsupported_firmware_disables_config(board, fw):
    fake, prog = board(fw=fw)
    with pytest.raises(SafetyRefusal, match="not supported"):
        prog.plan_set({"l_current_max": "50"})
    assert p.COMM_GET_MCCONF not in fake.commands


def test_wrong_signature_is_refused(board):
    fake, prog = board()
    fake.mc = b"\x00\x00\x00\x01" + fake.mc[4:]
    with pytest.raises(SafetyRefusal, match="signature"):
        prog.plan_set({"l_current_max": "50"})


def test_wrong_length_config_is_refused(board):
    fake, prog = board()
    fake.mc = fake.mc[:-1]
    with pytest.raises(SafetyRefusal, match="bytes"):
        prog.plan_set({"l_current_max": "50"})


def test_config_changing_under_us_is_refused(board):
    fake, prog = board(change_mc_on_read=True)
    with pytest.raises(SafetyRefusal, match="between two reads"):
        prog.plan_set({"l_current_max": "50"})


def test_config_changed_after_planning_is_refused(board):
    fake, prog = board()
    plan = prog.plan_set({"l_current_max": "50"})
    fake.mc = config.patch_blob(fake.mc, LAYOUT, MC_ROOT, {"l_max_erpm": 1234.0})
    with pytest.raises(SafetyRefusal, match="since this write was planned"):
        prog.commit(plan)
    assert fake.written == []


def test_no_write_if_the_backup_cannot_be_saved(board, tmp_path):
    fake, prog = board()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    prog.backup_dir = blocker / "sub"
    plan = prog.plan_set({"l_current_max": "50"})
    with pytest.raises(SafetyRefusal, match="could not save a backup"):
        prog.commit(plan)
    assert fake.written == []


def test_script_overrides_are_not_stored_silently(board):
    fake, prog = board()
    prog.save_backup()
    fake.mc = config.patch_blob(fake.mc, LAYOUT, MC_ROOT, {"l_max_erpm": 1234.0})
    with pytest.raises(SafetyRefusal, match="l_max_erpm"):
        prog.plan_set({"l_current_max": "50"})
    assert prog.plan_set({"l_current_max": "50"}, accept_drift=True).diff


# ------------------------------------------------------------------------- failures during a write
def test_lost_reply_is_judged_by_read_back_and_not_retried(board):
    fake, prog = board(write_mode="drop_ack")
    res = write(prog, l_current_max=50)
    assert res["acked"] is False and fake.value("l_current_max") == 50
    assert len(fake.written) == 1


@pytest.mark.parametrize("mode", ["ignored", "reject"])
def test_ignored_write_is_reported_and_not_retried(board, mode):
    fake, prog = board(write_mode=mode)
    with pytest.raises(VerifyFailed, match="old config"):
        write(prog, l_current_max=50)
    assert len(fake.written) == 1


def test_board_dying_mid_write_reports_unknown_state_and_keeps_backup(board):
    fake, prog = board(write_mode="dead")
    with pytest.raises(CommsError, match="Don't retry"):
        write(prog, l_current_max=50)
    assert len(fake.written) == 1
    assert [x for x in backups(prog) if "pre-write" in x.name]


def test_firmware_clamp_is_reported_without_rollback(board):
    fake, prog = board(clamp={"l_current_max": 100.0})
    res = write(prog, l_current_max=120)
    assert fake.value("l_current_max") == 100
    assert res["limited"]["l_current_max"] == (120.0, 100.0)
    assert len(fake.written) == 1


def test_unexpected_change_is_rolled_back_once(board):
    fake, prog = board(side_effect={"si_motor_poles": 14})
    original = fake.mc
    with pytest.raises(VerifyFailed, match="rolled back"):
        write(prog, l_current_max=50)
    assert fake.mc == original
    assert len(fake.written) == 2


def test_failed_rollback_is_reported_and_stops(board):
    fake, prog = board(side_effect={"si_motor_poles": 14}, side_effect_writes=2)
    with pytest.raises(VerifyFailed, match="rollback did not fully apply"):
        write(prog, l_current_max=50)
    assert len(fake.written) == 2


# ------------------------------------------------------------------------- backups and restore
def test_damaged_backup_is_refused(board):
    fake, prog = board()
    path = prog.save_backup()
    doc = json.loads(path.read_text())
    doc["mcconf"]["b64"] = "AAAA" + doc["mcconf"]["b64"][4:]
    path.write_text(json.dumps(doc))
    with pytest.raises(SafetyRefusal, match="damaged"):
        backup.load(path)


def _doc_from(fake_kwargs, tmp_path):
    from sky_programmer.link import Link
    from sky_programmer.programmer import Programmer

    other = FakeVesc(**fake_kwargs)
    prog = Programmer(Link(other, clock=other.clock, sleep=other.clock.sleep), tmp_path / "other", say=lambda *_: None)
    return backup.load(prog.save_backup())


def test_backup_from_another_board_needs_the_flag(board, tmp_path):
    fake, prog = board()
    doc = _doc_from({"uuid": bytes(12), "values": {"l_current_max": 33.0}}, tmp_path)
    with pytest.raises(SafetyRefusal, match="another board"):
        prog.plan_restore(doc)
    prog.commit(prog.plan_restore(doc, allow_other_board=True))
    assert fake.value("l_current_max") == 33


def test_restore_keeps_this_boards_calibration(board, tmp_path):
    fake, prog = board()
    doc = _doc_from({"uuid": bytes(12), "values": {"l_current_max": 33.0, "foc_offsets_current[0]": 1000.0}}, tmp_path)
    prog.commit(prog.plan_restore(doc, allow_other_board=True))
    assert fake.value("foc_offsets_current[0]") == 2048


def test_backup_from_other_hardware_is_refused_even_with_the_flag(board, tmp_path):
    fake, prog = board()
    doc = _doc_from({"hw": "A200S", "uuid": bytes(12)}, tmp_path)
    with pytest.raises(SafetyRefusal, match="hardware"):
        prog.plan_restore(doc, allow_other_board=True)


def test_restore_refuses_invalid_config(board, tmp_path):
    fake, prog = board()
    doc = _doc_from({"values": {"foc_motor_flux_linkage": -2.0}}, tmp_path)
    with pytest.raises(SafetyRefusal, match="negative"):
        prog.plan_restore(doc)
    assert fake.written == []


# ------------------------------------------------------------------------- stop
def test_stop_pauses_and_releases(board):
    fake, prog = board()
    assert prog.stop() is True
    assert fake.script_running is False
    assert fake.commands[-1] == p.COMM_SET_CURRENT
