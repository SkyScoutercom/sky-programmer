"""The write path, against a simulated board, including the failures that could leave it damaged."""
import json

import pytest

from sky_programmer import backup, config
from sky_programmer import protocol as p
from sky_programmer.config import MC_ROOT
from sky_programmer.errors import CommsError, SafetyRefusal, VerifyFailed
from sky_programmer.simulator import LAYOUT, FakeVesc


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
    changed = [i for i, (a, b) in enumerate(zip(before, fake.written[0], strict=True)) if a != b]
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
    original = config.decode(fake.mc, LAYOUT, MC_ROOT)
    write(prog, l_current_max=50)
    pre = next(x for x in backups(prog) if "pre-write" in x.name)
    prog.commit(prog.plan_restore(backup.load(pre)))
    assert not config.differences(original, config.decode(fake.mc, LAYOUT, MC_ROOT), LAYOUT)


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
    with pytest.raises(SafetyRefusal, match="firmware default"):
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
    plan = prog.plan_set({"l_current_max": "50"})
    prog.backup_dir = blocker / "sub"
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
    res = write(prog, l_current_max=110)
    assert fake.value("l_current_max") == 100
    assert res["limited"]["l_current_max"] == (110.0, 100.0)
    assert len(fake.written) == 1


def test_unexpected_change_is_rolled_back_once(board):
    fake, prog = board(side_effect={"si_motor_poles": 14})
    original = config.decode(fake.mc, LAYOUT, MC_ROOT)
    with pytest.raises(VerifyFailed, match="rolled back"):
        write(prog, l_current_max=50)
    assert not config.differences(original, config.decode(fake.mc, LAYOUT, MC_ROOT), LAYOUT)
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
    assert fake.released is True


def test_stop_reports_when_there_is_no_script(board):
    fake, prog = board(script_present=False)
    assert prog.stop() is False
    assert fake.released is True


def test_stop_still_releases_the_motor_if_the_pause_fails(board):
    fake, prog = board(pause_fails=True)
    with pytest.raises(SafetyRefusal, match="did not pause"):
        prog.stop()
    assert fake.released is True


# ------------------------------------------------------------------------- findings of the independent review
@pytest.mark.parametrize("changes", [
    {"m_bldc_f_sw_min": "0", "m_bldc_f_sw_max": "0"}, {"m_bldc_f_sw_min": "30", "m_bldc_f_sw_max": "30"},
    {"m_bldc_f_sw_min": "2563", "m_bldc_f_sw_max": "2563"},
    {"m_bldc_f_sw_min": "3000000", "m_bldc_f_sw_max": "3000000"},
    {"m_bldc_f_sw_max": "41000"}, {"m_dc_f_sw": "0"}, {"m_dc_f_sw": "26000"},
])  # fmt: skip
def test_switching_frequency_that_hangs_boot_is_refused_even_with_every_flag(board, changes):
    fake, prog = board()
    with pytest.raises(SafetyRefusal):
        prog.plan_set(changes, accept_drift=True, allow_default_foc=True, allow_risky=True)
    assert fake.written == []


def test_good_switching_frequency_survives_power_cycle(board):
    fake, prog = board()
    write(prog, m_bldc_f_sw_min=20000)
    fake.power_cycle()
    assert not fake.boot_hung and fake.value("m_bldc_f_sw_min") == 20000


def test_a_hung_board_stays_hung_in_the_simulator(board):
    """Proves the simulator would show the failure if a bad value ever got through."""
    fake, prog = board()
    fake.mc = config.patch_blob(fake.mc, LAYOUT, MC_ROOT, {"m_bldc_f_sw_min": 0.0})
    fake.power_cycle()
    assert fake.boot_hung


def test_cannot_enter_bldc_with_a_switching_frequency_that_hangs_boot(board):
    fake, prog = board(values={"motor_type": 2, "m_bldc_f_sw_min": 0.0, "m_bldc_f_sw_max": 0.0})
    with pytest.raises(SafetyRefusal, match="hang at boot"):
        prog.plan_set({"motor_type": "BLDC"}, accept_drift=True)


def test_restore_cannot_bring_back_a_switching_frequency_that_hangs_boot(board, tmp_path):
    fake, prog = board()
    doc = _doc_from({"values": {"m_bldc_f_sw_min": 0.0}}, tmp_path)
    with pytest.raises(SafetyRefusal):
        prog.plan_restore(doc, allow_risky=True)
    assert fake.written == []


def test_values_outside_vesc_tool_ranges_are_refused(board):
    fake, prog = board()
    with pytest.raises(SafetyRefusal, match="outside its range"):
        prog.plan_set({"sl_bemf_coupling_k": "6000"})


@pytest.mark.parametrize("field,value", [
    ("m_drv8301_oc_mode", "DISABLED"), ("foc_short_ls_on_zero_duty", "1"), ("l_max_erpm_fbrake", "100000"),
    ("l_temp_motor_end", "150"), ("l_max_vin", "140"), ("l_current_max", "130"), ("l_abs_current_max", "300"),
])  # fmt: skip
def test_risky_changes_need_the_flag(board, field, value):
    fake, prog = board()
    with pytest.raises(SafetyRefusal, match="allow-risky"):
        prog.plan_set({field: value})
    assert prog.plan_set({field: value}, allow_risky=True).diff


def test_unknown_board_needs_the_flag_to_raise_current_but_not_lower_it(board):
    fake, prog = board(hw="SomethingElse")
    with pytest.raises(SafetyRefusal, match="rating this app"):
        prog.plan_set({"l_current_max": "71"})
    assert prog.plan_set({"l_current_max": "50"}).diff


def test_status_style_reads_do_not_resume_a_paused_script(board):
    fake, prog = board()
    prog.stop()
    prog.identify(), prog.values(), prog.read_conf(MC_ROOT)
    assert fake.script_running is False
    assert 134 not in fake.commands


def test_meta_commands_do_not_resume_but_expressions_do(board):
    fake, prog = board()
    prog.stop()
    prog.repl(":ctxs")
    assert fake.script_running is False
    prog.repl("(+ 1 2)")
    assert fake.script_running is True  # why the app warns about this


def test_turning_engine_never_loses_its_script_to_a_write(board):
    fake, prog = board()
    plan = prog.plan_set({"l_current_max": "50"})
    fake.erpm = 100000
    with pytest.raises(SafetyRefusal, match="not idle"):
        prog.commit(plan)
    assert fake.script_running is True
    assert p.COMM_LISP_SET_RUNNING not in fake.commands


def test_refusal_after_the_pause_says_the_script_is_paused(board):
    fake, prog = board(spin_after_pause=3000)
    plan = prog.plan_set({"l_current_max": "50"})
    with pytest.raises(SafetyRefusal, match="now paused"):
        prog.commit(plan)


def test_one_measured_value_is_not_enough_for_foc(board):
    fake, prog = board(values={"foc_motor_r": 0.015, "foc_motor_l": 7.5e-6, "foc_motor_flux_linkage": 0.00245})
    with pytest.raises(SafetyRefusal, match="foc_motor_l, foc_motor_flux_linkage"):
        prog.plan_set({"motor_type": "FOC", "foc_motor_r": "0.02"})


def test_first_write_without_a_baseline_is_refused(board):
    fake, prog = board(baseline=False)
    with pytest.raises(SafetyRefusal, match="no SKY backup"):
        prog.plan_set({"l_current_max": "50"})
    assert prog.plan_set({"l_current_max": "50"}, accept_drift=True).diff


def test_truncation_of_unrelated_fields_is_not_a_failed_write(board):
    """foc_sl_openloop_time = 2.09 reads back as 2.08 on real firmware: no false rollback."""
    fake, prog = board(values={"foc_sl_openloop_time": 2.09})
    res = write(prog, l_current_max=50)
    assert len(fake.written) == 1
    assert fake.value("l_current_max") == 50 and res["limited"] == {}


def test_requested_value_losing_a_step_is_not_reported_as_limited(board):
    fake, prog = board()
    res = write(prog, foc_sl_openloop_time=0.53)
    assert res["limited"] == {} and len(fake.written) == 1


def test_firmware_linked_field_does_not_cause_a_rollback(board):
    fake, prog = board(values={"foc_sl_erpm": 4000.0, "foc_sl_erpm_start": 3000.0}, linked_limit=True)
    res = write(prog, foc_sl_erpm=2000)
    assert len(fake.written) == 1
    assert res["limited"]["foc_sl_erpm_start"][1] == pytest.approx(1800)


def test_failed_pause_stops_the_write(board):
    fake, prog = board(pause_fails=True)
    plan = prog.plan_set({"l_current_max": "50"})
    with pytest.raises(SafetyRefusal, match="did not pause"):
        prog.commit(plan)
    assert fake.written == []


def test_write_works_with_no_script_on_the_board(board):
    fake, prog = board(script_present=False)
    write(prog, l_current_max=50)
    assert fake.value("l_current_max") == 50


def test_unreadable_read_back_is_not_reported_as_nothing_written(board):
    fake, prog = board()
    plan = prog.plan_set({"l_current_max": "50"})
    original = fake._write_config

    def corrupt(blob):
        original(blob)
        fake.mc = fake.mc[:-1]

    fake._write_config = corrupt
    with pytest.raises(VerifyFailed):
        prog.commit(plan)


def test_new_voltage_range_cannot_lock_out_later_writes(board):
    fake, prog = board()
    with pytest.raises(SafetyRefusal, match="present bus voltage"):
        prog.plan_set({"l_min_vin": "60"})


def test_huge_number_is_refused_cleanly(board):
    fake, prog = board()
    with pytest.raises(SafetyRefusal):
        prog.plan_set({"foc_motor_r": "1e39"})


def test_repl_refused_on_unsupported_firmware(board):
    fake, prog = board(fw=(6, 6))
    with pytest.raises(SafetyRefusal, match="not supported"):
        prog.repl("(get-vin)")
    assert p.COMM_LISP_REPL_CMD not in fake.commands
