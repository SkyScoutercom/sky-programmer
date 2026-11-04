"""The script-state record and guards that the terminal and desktop apps share."""
import os
import pty

import pytest

from sky_programmer import backup, config
from sky_programmer.errors import CommsError, SafetyRefusal, VerifyFailed
from sky_programmer.link import open_serial


def test_nothing_else_can_open_the_port_while_it_is_in_use():
    master, slave = pty.openpty()
    path = os.ttyname(slave)
    ser = open_serial(path)
    try:
        assert ser.write_timeout is not None
        with pytest.raises(OSError):
            os.close(os.open(path, os.O_RDWR | os.O_NOCTTY))
    finally:
        ser.close()
    try:  # released on close, even though something else still holds the port
        open_serial(path).close()
    finally:
        os.close(slave)
        os.close(master)


def test_a_new_session_does_not_claim_the_script_is_paused(board):
    _, prog = board()
    assert prog.script_paused is False


def test_stop_records_the_pause(board):
    fake, prog = board()
    assert prog.stop() is True
    assert prog.script_paused is True
    assert fake.script_running is False


def test_stop_with_no_script_records_no_pause(board):
    _, prog = board(script_present=False)
    assert prog.stop() is False
    assert prog.script_paused is False


def test_a_timed_out_pause_counts_as_maybe_paused_because_the_firmware_keeps_it_pending(board):
    fake, prog = board(pause_fails=True)
    with pytest.raises(SafetyRefusal):
        prog.stop()
    assert fake.pause_pending is True
    assert prog.script_paused is True
    assert fake.released is True  # the motor is still released


def test_a_lost_pause_reply_counts_as_maybe_paused(board):
    fake, prog = board(lose_running_reply=True)
    with pytest.raises(CommsError):
        prog.stop()
    assert fake.script_running is False
    assert prog.script_paused is True


def test_a_lost_start_reply_keeps_saying_maybe_paused(board):
    fake, prog = board()
    prog.stop()
    fake.lose_running_reply = True
    with pytest.raises(CommsError):
        prog.start()
    assert prog.script_paused is True


def test_repl_that_could_not_pause_keeps_the_record(board):
    fake, prog = board()
    prog.stop()
    fake.repl_cannot_pause = True
    assert prog.repl("(+ 1 2)") == ["Could not pause"]
    assert prog.script_paused is True


def test_repl_with_no_reply_keeps_the_record(board, monkeypatch):
    fake, prog = board()
    prog.stop()
    monkeypatch.setattr(prog.link, "collect_prints", lambda wait: [])
    prog.repl("(+ 1 2)")
    assert prog.script_paused is True


def test_forget_pause_clears_the_record(board):
    _, prog = board()
    prog.stop()
    prog.forget_pause()
    assert prog.script_paused is False


def test_start_clears_the_record(board):
    _, prog = board()
    prog.stop()
    assert prog.start() is True
    assert prog.script_paused is False


def test_failed_start_keeps_the_record(board):
    fake, prog = board()
    prog.stop()
    fake.script_present = False
    assert prog.start() is False
    assert prog.script_paused is True


def test_a_repl_expression_clears_the_record_because_the_firmware_resumes(board):
    fake, prog = board()
    prog.stop()
    prog.repl("(+ 1 2)")
    assert fake.script_running is True
    assert prog.script_paused is False


def test_a_repl_meta_command_keeps_the_record(board):
    fake, prog = board()
    prog.stop()
    prog.repl(":info")
    assert fake.script_running is False
    assert prog.script_paused is True


def test_a_refused_repl_expression_keeps_the_record(board):
    fake, prog = board()
    prog.stop()
    with pytest.raises(SafetyRefusal):
        prog.repl("(conf-store)")
    assert prog.script_paused is True
    assert fake.script_running is False


def test_a_write_records_the_pause(board):
    _, prog = board()
    plan = prog.plan_set({"l_temp_fet_start": "84"})
    prog.commit(plan)
    assert prog.script_paused is True


def test_a_turning_engine_refused_before_the_pause_records_nothing(board):
    fake, prog = board()
    plan = prog.plan_set({"l_temp_fet_start": "84"})
    fake.erpm = 5000
    with pytest.raises(SafetyRefusal):
        prog.commit(plan)
    assert prog.script_paused is False
    assert fake.script_running is True


def test_check_can_stop_refuses_while_turning_unless_forced(board):
    fake, prog = board()
    fake.erpm = 5000
    with pytest.raises(SafetyRefusal, match="turning"):
        prog.check_can_stop()
    prog.check_can_stop(force=True)


def test_check_can_start_refuses_while_turning(board):
    fake, prog = board()
    fake.erpm = 5000
    with pytest.raises(SafetyRefusal, match="turning"):
        prog.check_can_start()


def test_a_failed_restore_does_not_launder_drift_into_the_baseline(board):
    fake, prog = board()
    doc = backup.load(prog.save_backup())
    fake.mc = config.patch_blob(fake.mc, prog.layout(), config.MC_ROOT, {"l_current_max": 20.0})  # a script's change
    with pytest.raises(SafetyRefusal, match="differs"):
        prog.plan_set({"l_temp_fet_start": "84"})
    fake.write_mode = "ignored"
    plan = prog.plan_restore(doc)
    with pytest.raises(VerifyFailed):
        prog.commit(plan)  # saves a pre-write backup holding the drifted value, then fails
    fake.write_mode = "ok"
    with pytest.raises(SafetyRefusal, match="differs"):
        prog.plan_set({"l_temp_fet_start": "84"})


def test_baseline_drift_reports_a_scripts_change(board):
    fake, prog = board()
    assert prog.baseline_drift() == {}
    fake.mc = config.patch_blob(fake.mc, prog.layout(), config.MC_ROOT, {"l_current_max": 20.0})
    assert set(prog.baseline_drift()) == {"l_current_max"}


def test_no_baseline_means_no_drift_to_report(board):
    _, prog = board(baseline=False)
    assert prog.baseline() is None
    assert prog.baseline_drift() == {}


def test_supply_is_checked_again_at_write_time(board):
    fake, prog = board()
    plan = prog.plan_set({"l_max_vin": "60"})
    fake.v_in = 65.0
    with pytest.raises(SafetyRefusal, match="outside"):
        prog.commit(plan)
    assert fake.written == []


def test_a_dead_board_after_a_write_names_the_backup(board):
    fake, prog = board(write_mode="dead")
    plan = prog.plan_set({"l_temp_fet_start": "84"})
    with pytest.raises(CommsError, match="Backup: .*pre-write"):
        prog.commit(plan)


@pytest.mark.parametrize("text", ["[]", "3", '{"format": 1, "app": "SKY programmer"}',
                                  '{"format": 1, "app": "SKY programmer", "mcconf": 3, "appconf": 4}'])
def test_damaged_backup_files_are_refused_not_crashed_on(tmp_path, text):
    bad = tmp_path / "bad.json"
    bad.write_text(text)
    with pytest.raises(SafetyRefusal):
        backup.load(bad)


def test_a_damaged_backup_does_not_stop_finding_the_baseline(board, tmp_path):
    _, prog = board()
    ident = prog.identify()
    folder = tmp_path / "backups" / ident["uuid"]
    (folder / "99999999-000000-000000000-manual.json").write_text("[]")
    assert prog.baseline() is not None


def test_show_pair_never_hides_a_real_change(board):
    from sky_programmer.config import MC_ROOT, show_pair

    _, prog = board()
    left, right = show_pair(prog.layout(), MC_ROOT, "l_max_erpm", 100000.0, 100000.4)
    assert left != right


def test_compare_backup_reports_differences(board, tmp_path):
    _, prog = board()
    doc = backup.load(prog.save_backup())
    assert prog.compare_backup(doc) == {}
    prog.commit(prog.plan_set({"l_temp_fet_start": "84"}))
    assert set(prog.compare_backup(doc)) == {"l_temp_fet_start"}


def test_compare_backup_refuses_other_firmware(board):
    _, prog = board()
    doc = backup.load(prog.save_backup())
    doc["fw"] = "6.05"
    with pytest.raises(SafetyRefusal, match="firmware"):
        prog.compare_backup(doc)
