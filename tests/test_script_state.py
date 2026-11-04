"""The script-state record and guards that the terminal and desktop apps share."""
import pytest

from sky_programmer import backup
from sky_programmer.errors import SafetyRefusal


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


def test_a_failed_pause_is_not_recorded_as_paused(board):
    fake, prog = board(pause_fails=True)
    with pytest.raises(SafetyRefusal):
        prog.stop()
    assert prog.script_paused is False
    assert fake.released is True  # the motor is still released


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
