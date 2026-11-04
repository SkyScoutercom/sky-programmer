"""The real `sky` command and real pyserial, talking over a pseudo-terminal to the simulated board."""
import os
import sys
import threading
import time
import tty

import pytest

from fake_vesc import FakeVesc
from sky_programmer.cli import main


class PtyBoard:
    def __init__(self, **knobs):
        self.fake = FakeVesc(clock=time.monotonic, **knobs)
        self.fake.read = self._read
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.port = os.ttyname(self.slave)
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _read(self, n):
        out, self.fake.tx = self.fake.tx[:n], self.fake.tx[n:]
        return out

    def _run(self):
        os.set_blocking(self.master, False)
        while self.running:
            try:
                self.fake.write(os.read(self.master, 65536))
            except BlockingIOError:
                pass
            out = self._read(65536)
            if out:
                os.write(self.master, out)
            time.sleep(0.001)

    def close(self):
        self.running = False
        self.thread.join()
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def run(tmp_path, monkeypatch):
    boards = []

    def _run(*argv, pty=None, **knobs):
        pty = pty or PtyBoard(**knobs)
        if pty not in boards:
            boards.append(pty)
        return main(["--port", pty.port, "--backup-dir", str(tmp_path / "bk"), *argv]), pty.fake

    yield _run
    for b in boards:
        b.close()
        assert b.fake.violations == []


def test_status(run, capsys):
    code, _ = run("status")
    out = capsys.readouterr().out
    assert code == 0
    assert "Flipsky_110_100" in out and "7.00" in out and "MOTOR_TYPE_BLDC" in out and "FAULT_CODE_NONE" in out


def test_status_on_unsupported_firmware_still_reads(run, capsys):
    code, _ = run("status", fw=(6, 6))
    out = capsys.readouterr().out
    assert code == 0 and "6.06" in out and "not supported" in out


def test_get_one_field(run, capsys):
    code, _ = run("get", "motor_type")
    assert code == 0 and "motor_type = MOTOR_TYPE_BLDC" in capsys.readouterr().out


def test_get_wildcard(run, capsys):
    code, _ = run("get", "foc_motor_*")
    out = capsys.readouterr().out
    assert code == 0 and "foc_motor_r = 0.0081" in out


def test_set_to_foc_end_to_end(run, capsys):
    pty = PtyBoard()
    assert run("backup", pty=pty)[0] == 0
    code, fake = run("set", "motor_type=FOC", "--engine-stopped", pty=pty)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "MOTOR_TYPE_BLDC -> MOTOR_TYPE_FOC" in out and "reads back correct" in out and "Backup saved" in out
    assert "power-cycle" in out
    assert fake.value("motor_type") == 2


def test_set_without_statement_and_no_terminal_is_refused(run, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    code, fake = run("set", "motor_type=FOC", "--accept-drift")
    assert code == 2 and fake.written == []
    assert "--engine-stopped" in capsys.readouterr().err


class _Tty:
    def isatty(self):
        return True


@pytest.mark.parametrize("answer,written", [("YES", 1), ("yes", 0), ("y", 0), ("", 0)])
def test_only_typing_yes_confirms_a_write(run, capsys, monkeypatch, answer, written):
    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    code, fake = run("set", "l_current_max=50", "--accept-drift")
    assert len(fake.written) == written and code == (0 if written else 2)


def test_end_of_input_at_the_prompt_is_a_no(run, capsys, monkeypatch):
    def eof(*_):
        raise EOFError

    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr("builtins.input", eof)
    code, fake = run("set", "l_current_max=50", "--accept-drift")
    assert code == 2 and fake.written == []


def test_no_banner_or_colour_codes_in_piped_output(run, capsys):
    run("status")
    assert "\033[" not in capsys.readouterr().out


def test_banner_in_a_terminal_uses_the_logo_colours(run, capsys, monkeypatch):
    real = sys.stdout

    class Term:
        def isatty(self):
            return True

        def write(self, text):
            return real.write(text)

        def flush(self):
            real.flush()

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stdout", Term())
    run("status")
    out = capsys.readouterr().out
    assert "SKY programmer" in out and "38;2;2;149;224" in out and "38;2;0;85;142" in out


def test_no_color_is_respected(run, capsys, monkeypatch):
    real = sys.stdout

    class Term:
        def isatty(self):
            return True

        def write(self, text):
            return real.write(text)

        def flush(self):
            real.flush()

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("sys.stdout", Term())
    run("status")
    out = capsys.readouterr().out
    assert "SKY programmer" in out and "\033[" not in out


def test_dry_run_writes_nothing(run, capsys):
    code, fake = run("set", "motor_type=FOC", "--dry-run", "--accept-drift")
    assert code == 0 and fake.written == [] and "Dry run" in capsys.readouterr().out


def test_refusal_exit_code_and_message(run, capsys):
    code, fake = run("set", "foc_motor_flux_linkage=-2", "--engine-stopped")
    assert code == 2 and fake.written == [] and "negative" in capsys.readouterr().err


def test_already_set_writes_nothing(run, capsys):
    code, fake = run("set", "l_current_max=70", "--engine-stopped")
    assert code == 0 and fake.written == [] and "Already set" in capsys.readouterr().out


def test_stop_refused_while_engine_may_be_running(run, capsys):
    pty = PtyBoard()
    pty.fake.erpm = 140000
    code, fake = run("stop", pty=pty)
    assert code == 2 and fake.script_running is True and "throttle" in capsys.readouterr().err
    fake.erpm = 0


def test_start_refused_while_turning(run, capsys):
    pty = PtyBoard(script_present=True)
    pty.fake.script_running = False
    pty.fake.erpm = 5000
    code, fake = run("start", "--engine-stopped", pty=pty)
    assert code == 2 and fake.script_running is False
    fake.erpm = 0


def test_status_does_not_resume_a_paused_script(run, capsys):
    pty = PtyBoard()
    assert run("stop", pty=pty)[0] == 0
    assert run("status", pty=pty)[0] == 0
    assert pty.fake.script_running is False


def test_status_works_with_no_script(run, capsys):
    code, _ = run("status", script_present=False)
    assert code == 0 and "MOTOR_TYPE_BLDC" in capsys.readouterr().out


def test_repl_warns_about_resuming(run, capsys):
    code, _ = run("repl", "-e", "(+ 1 2)")
    assert code == 0 and "resumes a script" in capsys.readouterr().err


def test_terminal_read_only(run, capsys):
    code, _ = run("term", "faults")
    assert code == 0 and "fault log empty" in capsys.readouterr().out


def test_terminal_refuses_action(run, capsys):
    code, _ = run("term", "rebootwdt")
    assert code == 2


def test_repl_expression(run, capsys):
    code, _ = run("repl", "-e", "(get-adc 0)")
    assert code == 0 and "(get-adc 0)" in capsys.readouterr().out


def test_repl_refuses_flash_word(run, capsys):
    code, fake = run("repl", "-e", "(conf-store)")
    assert code == 2 and 138 not in fake.commands


def test_backup_diff_restore_cycle(run, capsys, tmp_path):
    pty = PtyBoard()
    assert run("backup", pty=pty)[0] == 0
    path = capsys.readouterr().out.split("Backup saved: ")[1].strip()
    assert run("set", "l_current_max=50", "--engine-stopped", pty=pty)[0] == 0
    capsys.readouterr()
    assert run("diff", path, pty=pty)[0] == 0
    assert "l_current_max" in capsys.readouterr().out
    assert run("restore", path, "--engine-stopped", pty=pty)[0] == 0
    assert pty.fake.value("l_current_max") == 70


def test_monitor_writes_csv(run, capsys, tmp_path):
    log = tmp_path / "m.csv"
    code, _ = run("monitor", "--seconds", "0.3", "--log", str(log))
    assert code == 0
    assert log.read_text().splitlines()[0].startswith("t,erpm,duty")


def test_no_board_found(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("glob.glob", lambda *_: [])
    assert main(["--backup-dir", str(tmp_path), "status"]) == 3
    assert "no VESC found" in capsys.readouterr().err


def test_two_boards_are_never_guessed(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("glob.glob", lambda *_: ["/dev/ttyACM0", "/dev/ttyACM1"])
    assert main(["--backup-dir", str(tmp_path), "status"]) == 2
