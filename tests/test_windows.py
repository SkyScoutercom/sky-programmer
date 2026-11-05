"""The Windows paths through the code. These run on every system, with Windows' answers swapped in."""
import os
import sys
from types import SimpleNamespace

import pytest
from serial.tools import list_ports as serial_ports  # imported here, before a test pretends to be on Windows

from sky_programmer import link, paths, safety
from sky_programmer.errors import CommsError, SafetyRefusal


@pytest.fixture
def windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    return tmp_path


def _comports(monkeypatch, *ports):
    fake = [SimpleNamespace(device=device, vid=vid) for device, vid in ports]
    monkeypatch.setattr(serial_ports, "comports", lambda: fake)


def test_files_go_in_the_windows_app_folders(windows):
    assert paths.data_dir() == windows / "local" / "sky-programmer"
    assert paths.config_dir() == windows / "roaming" / "sky-programmer"


def test_backups_and_logs_use_those_folders(windows):
    from sky_programmer import backup, gitops

    assert backup.default_dir() == windows / "local" / "sky-programmer" / "backups"
    assert gitops.audit_path() == windows / "local" / "sky-programmer" / "git_ops.log"


def test_linux_paths_are_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    assert paths.data_dir() == tmp_path / "d" / "sky-programmer"
    assert paths.config_dir() == tmp_path / "c" / "sky-programmer"


def test_only_usb_com_ports_are_listed_so_bluetooth_ports_are_never_offered(windows, monkeypatch):
    _comports(monkeypatch, ("COM1", None), ("COM7", 0x0483), ("COM3", None))
    assert link.list_ports() == ["COM7"]


def test_com_ports_sort_by_number(windows, monkeypatch):
    _comports(monkeypatch, ("COM10", 0x0483), ("COM9", 0x0483), ("COM2", 0x0483))
    assert link.list_ports() == ["COM2", "COM9", "COM10"]


def test_several_usb_boards_are_never_guessed_between(windows, monkeypatch):
    _comports(monkeypatch, ("COM4", 0x0483), ("COM5", 0x0483))
    with pytest.raises(SafetyRefusal, match="more than one"):
        link.find_port()


def test_no_board_is_a_clear_error(windows, monkeypatch):
    _comports(monkeypatch)
    with pytest.raises(CommsError, match="no VESC found"):
        link.find_port()


def test_an_empty_firmware_word_list_refuses_rather_than_allowing_everything(monkeypatch, tmp_path):
    """A packaged build that left the layouts behind must not turn the REPL filter off."""
    safety.firmware_words.cache_clear()
    monkeypatch.setattr(safety.resources, "files", lambda package: tmp_path)
    try:
        with pytest.raises(SafetyRefusal, match="word list"):
            safety.check_repl("(cmds-proc 73)")
    finally:
        monkeypatch.undo()
        safety.firmware_words.cache_clear()
    with pytest.raises(SafetyRefusal):
        safety.check_repl("(cmds-proc 73)")


def test_the_repl_filter_still_allows_ordinary_expressions():
    safety.check_repl("(+ 1 2)")


def test_a_windows_style_port_name_reaches_the_serial_library(monkeypatch):
    """Windows has no fcntl, so the port must open with no POSIX lock calls."""
    import serial

    opened = {}

    class Fake:
        is_open = False

        def __init__(self, port, baud, **kwargs):
            opened.update(port=port, **kwargs)

        def close(self):
            pass

    monkeypatch.setattr(link, "fcntl", None)
    monkeypatch.setattr(serial, "Serial", Fake)
    ser = link.open_serial("COM7")
    assert opened["port"] == "COM7"
    assert opened["exclusive"] is True  # on Windows the system enforces this for every COM port
    assert opened["write_timeout"] == link.WRITE_TIMEOUT_S
    ser.close()


@pytest.mark.skipif(sys.platform == "win32", reason="this checks the source from the Linux side")
def test_the_engine_imports_with_no_posix_modules(tmp_path):
    """What Windows sees: no fcntl or termios at all."""
    import subprocess

    code = ("import sys\n"
            "sys.modules['fcntl'] = None\n"
            "sys.modules['termios'] = None\n"
            "from sky_programmer import cli, link, gitops, backup\n"
            "assert link.fcntl is None\n")  # fmt: skip
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    subprocess.run([sys.executable, "-c", code], check=True, env=env, cwd=tmp_path)
