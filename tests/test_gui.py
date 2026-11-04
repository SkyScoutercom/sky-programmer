"""The desktop app against the simulated board: the real window, the real worker thread, no real dialogs."""
import os
import subprocess
import threading
import time
from importlib import resources
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from sky_programmer import backup  # noqa: E402
from sky_programmer import protocol as p
from sky_programmer.gui import theme  # noqa: E402
from sky_programmer.gui.app import install_launcher  # noqa: E402
from sky_programmer.gui.config_page import NOTE, VALUE, NoWheelCombo  # noqa: E402
from sky_programmer.gui.demo import DemoBoard  # noqa: E402
from sky_programmer.gui.prompts import TypedConfirm  # noqa: E402
from sky_programmer.gui.window import MainWindow  # noqa: E402
from sky_programmer.gui.wording import NO_OVERRIDE, humanize  # noqa: E402
from sky_programmer.link import Link  # noqa: E402
from sky_programmer.simulator import FakeVesc  # noqa: E402

READ_ONLY = {p.COMM_FW_VERSION, p.COMM_GET_VALUES, p.COMM_GET_MCCONF, p.COMM_GET_APPCONF}


class FakePrompts:
    """Answers questions and remembers them."""

    def __init__(self):
        self.write_answer, self.start_answer, self.repl_answer, self.close_answer = True, True, True, True
        self.errors, self.infos, self.write_requests, self.differences = [], [], [], []
        self.starts, self.repl_asks, self.pauses = 0, 0, 0
        self.pause_answer, self.baseline_answer, self.reset_answer = True, True, True
        self.baseline_requests, self.resets = [], []
        self.power_cycled_answer = True

    def confirm_write(self, rows, warnings, target=""):
        self.write_requests.append((rows, warnings))
        self.write_target = target
        return self.write_answer

    def confirm_pause(self):
        self.pauses += 1
        return self.pause_answer

    def confirm_new_baseline(self, rows):
        self.baseline_requests.append(rows)
        return self.baseline_answer

    def confirm_reset(self, preview):
        self.resets.append(preview)
        return self.reset_answer

    def confirm_power_cycled(self):
        return self.power_cycled_answer

    def ask_folder(self, start):
        return getattr(self, "folder", "")

    def confirm_start(self):
        self.starts += 1
        return self.start_answer

    def confirm_repl_resume(self):
        self.repl_asks += 1
        return self.repl_answer

    def confirm_close_paused(self):
        return self.close_answer

    def error(self, title, text):
        self.errors.append((title, text))

    def info(self, title, text):
        self.infos.append((title, text))

    def show_differences(self, title, rows):
        self.differences.append(rows)

    def ask_csv_path(self):
        return getattr(self, "csv_path", "")


class CheckedBoard(FakeVesc):
    """Records which threads use the port, and can be unplugged."""

    def __init__(self, **knobs):
        super().__init__(**knobs)
        self.threads = set()
        self.unplugged = False

    def read(self, n):
        self.threads.add(threading.get_ident())
        if self.unplugged:
            raise OSError("device disconnected")
        return super().read(n)

    def write(self, data):
        self.threads.add(threading.get_ident())
        if self.unplugged:
            raise OSError("device disconnected")
        return super().write(data)


def wait_until(condition, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if condition():
            return
        time.sleep(0.002)
    raise AssertionError("timed out waiting for the app")


def _finish_queued_job(win):
    """False if there's no board to wait for."""
    wait_until(lambda: not win.board or not win.board.busy)
    done = []
    if not win.board or not win.board.submit(lambda prog: None, done.append):
        return False
    wait_until(lambda: done or not win.board)
    return True


def settle(win):
    """Wait for everything queued so far, including what its callbacks queue."""
    for _ in range(3):
        if not _finish_queued_job(win):
            return
        QApplication.processEvents()


@pytest.fixture
def gui(tmp_path):
    app = QApplication.instance() or QApplication([])
    made = []

    def make(baseline=True, connect=True, **knobs):
        fake = CheckedBoard(**knobs)
        prompts = FakePrompts()
        win = MainWindow(link_factory=lambda port: Link(fake, clock=fake.clock, sleep=fake.clock.sleep),
                         ports=lambda: ["/dev/ttyFAKE"], backup_dir=tmp_path / "backups", prompts=prompts,
                         settings_path=tmp_path / "settings.ini", repo_dir=tmp_path / "no-repo")
        win.show()
        made.append((win, fake))
        if baseline:
            helper = Link(fake, clock=fake.clock, sleep=fake.clock.sleep)
            from sky_programmer.programmer import Programmer

            try:
                Programmer(helper, tmp_path / "backups", say=lambda *_: None).save_backup()
            except Exception:
                pass  # tests of unsupported firmware
            fake.commands.clear()
            fake.threads.clear()
        if connect:
            win.connect_board()
            wait_until(lambda: win.info is not None or win.board is None)
            settle(win)
        return win, fake, prompts

    yield make
    for win, fake in made:
        win.prompts.close_answer = True
        wait_until(lambda w=win: not w.board or not w.board.busy)
        win.close()
        assert fake.violations == []
        assert threading.main_thread().ident not in fake.threads, "the GUI thread touched the port"
        win.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def edit_and_review(win, **fields):
    for field, text in fields.items():
        win.config_page.motor.set_value(field, text)
    win.config_page.review()
    settle(win)


# ---------------------------------------------------------------------- connecting
def test_connecting_only_reads(gui):
    win, fake, _ = gui()
    wait_until(lambda: fake.commands.count(p.COMM_GET_VALUES) > 5)
    assert set(fake.commands) <= READ_ONLY
    assert fake.written == []
    assert fake.script_running is True


def test_polling_sends_only_get_values(gui):
    win, fake, _ = gui()
    fake.commands.clear()
    wait_until(lambda: len(fake.commands) > 5)
    assert set(fake.commands) == {p.COMM_GET_VALUES}


def test_header_shows_the_board_and_mode(gui):
    win, _, _ = gui()
    assert "Flipsky_110_100" in win.board_label.text()
    assert "7.00" in win.board_label.text()
    assert "BLDC" in win.board_label.text()


def test_live_values_reach_the_dashboard(gui):
    win, fake, _ = gui()
    fake.v_in = 51.5
    wait_until(lambda: win.dashboard.tiles["bus"].value.text() == "51.5 V")
    assert win.dashboard.charts["v_in"].points


def test_charts_and_tiles_blank_when_values_stop(gui):
    win, fake, _ = gui()
    wait_until(lambda: win.dashboard.charts["v_in"].points)
    win.dashboard._last_sample = time.monotonic() - 10
    win.dashboard._check_stale()
    assert win.dashboard.tiles["bus"].value.text() == "—"
    assert all(chart.stale for chart in win.dashboard.charts.values())
    wait_until(lambda: not win.dashboard.charts["v_in"].stale)


def test_a_fault_is_logged_so_it_outlives_the_tile(gui):
    win, fake, _ = gui()
    fake.fault = 2
    wait_until(lambda: "UNDER_VOLTAGE" in win.console.output.toPlainText())
    fake.fault = 0
    wait_until(lambda: "NONE" in win.dashboard.tiles["fault"].value.text())
    assert "UNDER_VOLTAGE" in win.console.output.toPlainText()


def test_a_fault_is_shown_with_words_not_only_colour(gui):
    win, fake, _ = gui()
    fake.fault = 2
    wait_until(lambda: "⚠" in win.dashboard.tiles["fault"].value.text())
    assert win.dashboard.tiles["fault"].value.text() == "⚠ UNDER_VOLTAGE"


def test_unsupported_firmware_can_be_read_but_not_configured(gui):
    win, fake, prompts = gui(baseline=False, fw=(6, 5))
    assert win.info is not None and win.layout is None
    assert not win.config_page.review_button.isEnabled()
    assert not win.backups_page.backup_button.isEnabled()
    assert not win.note_banner.isHidden()
    assert "not supported" in win.note_banner.text.text()
    wait_until(lambda: win.dashboard.tiles["bus"].value.text() != "—")
    assert fake.written == []


def test_a_chosen_or_typed_port_is_never_changed_behind_your_back(gui):
    win, _, _ = gui(connect=False)
    win.port_combo.lineEdit().setText("/dev/ttyUSB3")
    win.port_combo.lineEdit().textEdited.emit("/dev/ttyUSB3")
    win.ports = lambda: ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2"]
    win.refresh_ports()
    assert win.port_combo.currentText() == "/dev/ttyUSB3"
    win.ports = lambda: ["/dev/ttyACM0"]
    win.refresh_ports()
    assert win.port_combo.currentText() == "/dev/ttyUSB3"


def test_preset_port_is_kept_even_if_it_is_not_listed(gui, tmp_path):
    win = MainWindow(ports=lambda: [], backup_dir=tmp_path, prompts=FakePrompts(), preset_port="/dev/ttyUSB7",
                     settings_path=tmp_path / "s.ini", repo_dir=tmp_path / "no-repo")
    assert win.port_combo.currentText() == "/dev/ttyUSB7"
    win.close()


def test_the_write_dialog_names_the_exact_board(gui):
    win, _, prompts = gui()
    edit_and_review(win, l_temp_fet_start="84")
    assert win.info is not None
    assert "0102030405060708090a0b0c" in prompts.write_target


def test_several_ports_are_never_guessed(gui):
    win, _, prompts = gui(connect=False)
    win.ports = lambda: ["/dev/ttyACM0", "/dev/ttyACM1"]
    win.refresh_ports()
    assert win.port_combo.currentText() == ""
    win.connect_board()
    assert win.board is None
    assert prompts.errors[-1][0] == "No port chosen"


def test_unplugging_closes_cleanly_and_says_so(gui):
    win, fake, prompts = gui()
    fake.unplugged = True
    wait_until(lambda: win.board is None)
    assert prompts.errors[-1][0] == "Disconnected"
    assert win.info is None
    assert win.stack.currentWidget() is win.welcome


def test_a_board_that_never_answers_reports_it(gui):
    win, fake, prompts = gui(connect=False, baseline=False)
    fake.dead = True
    win.connect_board()
    wait_until(lambda: win.board is None)
    assert prompts.errors[-1][0] == "Disconnected"


# ---------------------------------------------------------------------- writing
def test_write_goes_through_the_verified_path(gui):
    win, fake, prompts = gui()
    edit_and_review(win, l_temp_fet_start="84")
    assert fake.value("l_temp_fet_start") == 84
    assert len(fake.written) == 1
    rows, _ = prompts.write_requests[0]
    assert rows == [("l_temp_fet_start", "85", "84")]
    assert prompts.infos[-1][0] == "Written"
    assert "power-cycle" in prompts.infos[-1][1]
    assert len(list(Path(win.backup_dir).rglob("*pre-write.json"))) == 1


def test_only_the_edited_field_changes(gui):
    win, fake, _ = gui()
    before = dict(fake.__dict__)["mc"]
    edit_and_review(win, l_temp_fet_start="84")
    from sky_programmer import config

    a, b = config.decode(before, win.layout, "mc_configuration"), config.decode(fake.mc, win.layout, "mc_configuration")
    assert set(config.differences(a, b, win.layout)) == {"l_temp_fet_start"}


def test_cancelling_sends_no_pause_and_no_write(gui):
    win, fake, prompts = gui()
    prompts.write_answer = False
    edit_and_review(win, l_temp_fet_start="84")
    assert prompts.write_requests
    assert fake.written == []
    assert p.COMM_LISP_SET_RUNNING not in fake.commands
    assert fake.script_running is True
    assert win.config_page.motor.staged  # the edit is still there to fix or discard


def test_a_write_shows_the_paused_banner_and_start_clears_it(gui):
    win, fake, _ = gui()
    edit_and_review(win, l_temp_fet_start="84")
    settle(win)
    assert not win.paused_banner.isHidden()
    win.dashboard.start()
    settle(win)
    wait_until(lambda: win.paused_banner.isHidden())
    assert fake.script_running is True


def test_table_refreshes_after_a_write(gui):
    win, _, _ = gui()
    edit_and_review(win, l_temp_fet_start="84")
    wait_until(lambda: win.config_page.motor.original.get("l_temp_fet_start") == "84")
    assert not win.config_page.motor.staged


def test_bldc_to_foc_with_measured_values(gui):
    win, fake, prompts = gui()
    edit_and_review(win, motor_type="MOTOR_TYPE_FOC")
    wait_until(lambda: fake.value("motor_type") == 2)
    _, warnings = prompts.write_requests[0]
    assert any("calibration" in w for w in warnings)
    wait_until(lambda: "FOC" in win.board_label.text())


@pytest.mark.parametrize("field, value, why", [
    ("l_current_max", "130", "above this app's rating"),
    ("l_abs_current_max", "60", "would be invalid"),
])
def test_a_refused_plan_writes_nothing_and_offers_no_override(gui, field, value, why):
    win, fake, prompts = gui()
    edit_and_review(win, **{field: value})
    assert prompts.write_requests == []
    assert fake.written == []
    assert prompts.errors[-1][0] == "Refused"
    assert why in prompts.errors[-1][1]
    assert p.COMM_LISP_SET_RUNNING not in fake.commands


def test_refusal_tells_the_truth_about_overrides(gui):
    win, _, prompts = gui()
    edit_and_review(win, l_current_max="130")
    assert NO_OVERRIDE in prompts.errors[-1][1]


def test_boot_hanging_value_is_refused_even_when_planned_directly(gui):
    win, fake, prompts = gui()
    win.write_flow(lambda prog: prog.plan_set({"m_bldc_f_sw_min": "0"}))
    settle(win)
    assert fake.written == []
    assert prompts.errors[-1][0] == "Refused"


def test_boot_hanging_value_cannot_even_be_reviewed(gui):
    win, fake, _ = gui()
    table = win.config_page.motor
    table.set_value("m_bldc_f_sw_min", "0")
    assert "m_bldc_f_sw_min" in table.invalid
    assert not win.config_page.review_button.isEnabled()
    win.config_page.review()
    settle(win)
    assert fake.written == []


def test_turning_motor_refuses_a_write_and_keeps_the_script(gui):
    win, fake, prompts = gui()
    fake.erpm = 5000
    edit_and_review(win, l_temp_fet_start="84")
    assert prompts.write_requests == []
    assert prompts.errors[-1][0] == "Refused"
    assert "not idle" in prompts.errors[-1][1]
    assert fake.script_running is True


def test_motor_starting_during_the_dialog_still_refuses_the_write(gui):
    win, fake, prompts = gui()
    def spin_up(rows, warnings, target=""):
        fake.erpm = 5000
        return True
    prompts.confirm_write = spin_up
    edit_and_review(win, l_temp_fet_start="84")
    assert fake.written == []
    assert prompts.errors[-1][0] == "Refused"
    assert fake.script_running is True
    assert win.paused_banner.isHidden()


def test_config_changing_during_the_dialog_refuses_the_write(gui):
    win, fake, prompts = gui()
    def someone_else_writes(rows, warnings, target=""):
        from sky_programmer import config
        fake.mc = config.patch_blob(fake.mc, win.layout, "mc_configuration", {"l_max_erpm": 12345.0})
        return True
    prompts.confirm_write = someone_else_writes
    edit_and_review(win, l_temp_fet_start="84")
    assert fake.written == []
    assert prompts.errors[-1][0] == "Refused"
    wait_until(lambda: not win.paused_banner.isHidden())  # the pause did happen, and the operator must see that


def test_no_baseline_refuses_and_the_banner_fixes_it(gui):
    win, fake, prompts = gui(baseline=False)
    assert not win.baseline_banner.isHidden()
    edit_and_review(win, l_temp_fet_start="84")
    assert fake.written == []
    assert "Back up now" in prompts.errors[-1][1]
    win.backups_page.backup_now()
    settle(win)
    assert win.baseline_banner.isHidden()
    win.config_page.review()
    settle(win)
    assert fake.value("l_temp_fet_start") == 84


def test_a_write_the_board_ignores_is_reported_and_not_retried(gui):
    win, fake, prompts = gui(write_mode="ignored")
    edit_and_review(win, l_temp_fet_start="84")
    wait_until(lambda: prompts.errors)
    assert prompts.errors[-1][0] == "The write did not verify"
    assert len(fake.written) == 1


def test_a_board_dying_during_a_write_is_reported_and_not_retried(gui):
    win, fake, prompts = gui(write_mode="dead")
    edit_and_review(win, l_temp_fet_start="84")
    wait_until(lambda: prompts.errors)
    assert len(fake.written) == 1
    assert "Don't retry" in " ".join(t for _, t in prompts.errors)


def test_a_write_cannot_be_queued_twice(gui):
    win, fake, _ = gui()
    gate = threading.Event()
    assert win.board.submit(lambda prog: gate.wait(10), exclusive=True) is True
    assert win.board.submit(lambda prog: None, exclusive=True) is False
    gate.set()
    settle(win)


# ---------------------------------------------------------------------- the config table
def test_unchanged_values_are_not_staged(gui):
    win, _, _ = gui()
    table = win.config_page.motor
    for field in table.rows:
        table._edited(field)
    assert table.staged == {}
    assert table.invalid == {}


def test_protected_and_always_refused_fields_are_locked(gui):
    win, _, _ = gui()
    table = win.config_page.motor
    for field in ("foc_offsets_current[0]", "m_drv8301_oc_mode", "foc_short_ls_on_zero_duty", "l_temp_motor_start"):
        row = table.rows[field]
        assert table.cellWidget(row, VALUE) is None
        assert not table.item(row, VALUE).flags() & table.item(row, VALUE).flags().ItemIsEditable, field


def test_app_config_is_read_only(gui):
    win, _, _ = gui()
    table = win.config_page.app
    assert table.rows
    assert not [f for f, r in table.rows.items() if table.cellWidget(r, VALUE)
                or table.item(r, VALUE).flags() & table.item(r, VALUE).flags().ItemIsEditable]


def test_nonsense_input_is_caught_in_the_table(gui):
    win, _, _ = gui()
    table = win.config_page.motor
    table.set_value("l_temp_fet_start", "hot")
    assert "l_temp_fet_start" in table.invalid
    assert not win.config_page.review_button.isEnabled()


def test_table_is_disabled_during_a_write(gui):
    win, _, _ = gui()
    gate = threading.Event()
    win.board.submit(lambda prog: gate.wait(10), exclusive=True)
    QApplication.processEvents()
    assert not win.config_page.tabs.isEnabled()
    gate.set()
    settle(win)
    assert win.config_page.tabs.isEnabled()


def test_a_failed_read_clears_the_table_and_keeps_saying_so(gui):
    win, fake, _ = gui()
    page = win.config_page
    assert page.motor.rows
    fake.change_mc_on_read = True
    page.read()
    settle(win)
    wait_until(lambda: not page.motor.rows)
    assert "changed between two reads" in page.status.text()
    page._sync_buttons()
    assert "changed between two reads" in page.status.text()
    assert not page.review_button.isEnabled()
    assert page.read_button.isEnabled()


def test_a_value_outside_the_enum_is_shown_as_it_is(gui):
    win, fake, _ = gui(values={"foc_sensor_mode": 77})
    table = win.config_page.motor
    assert table.text_of("foc_sensor_mode") == "77"
    assert "isn't a valid choice" in table.item(table.rows["foc_sensor_mode"], NOTE).text()
    assert table.staged == {}


def test_hall_table_is_locked_with_its_one_byte_meaning(gui):
    win, _, _ = gui()
    table = win.config_page.motor
    row = table.rows["hall_table[0]"]
    assert table.cellWidget(row, VALUE) is None
    assert not table.item(row, VALUE).flags() & table.item(row, VALUE).flags().ItemIsEditable
    assert "255 means -1" in table.item(row, NOTE).text()


def test_phase_filter_fault_switch_is_locked(gui):
    win, _, _ = gui()
    table = win.config_page.motor
    assert table.cellWidget(table.rows["foc_phase_filter_disable_fault"], VALUE) is None


def test_scrolling_over_a_dropdown_cannot_change_a_setting(gui):
    win, _, _ = gui()
    combo = win.config_page.motor.cellWidget(win.config_page.motor.rows["motor_type"], VALUE)
    assert isinstance(combo, NoWheelCombo) and isinstance(combo, QComboBox)
    assert NoWheelCombo.wheelEvent is not QComboBox.wheelEvent


def test_search_finds_a_field_outside_the_group(gui):
    win, _, _ = gui()
    page, table = win.config_page, win.config_page.motor
    assert table.isRowHidden(table.rows["hall_table[0]"])
    page.search.setText("hall_table")
    assert not table.isRowHidden(table.rows["hall_table[0]"])
    assert table.isRowHidden(table.rows["motor_type"])


def test_reading_cannot_silently_drop_edits(gui):
    win, _, _ = gui()
    win.config_page.motor.set_value("l_temp_fet_start", "84")
    assert not win.config_page.read_button.isEnabled()
    assert win.config_page.discard_button.isEnabled()
    win.config_page.discard()
    settle(win)
    wait_until(lambda: not win.config_page.motor.staged)


# ---------------------------------------------------------------------- script controls
def test_pause_needs_the_operator_to_state_the_engine_is_stopped(gui):
    win, fake, prompts = gui()
    prompts.pause_answer = False
    win.dashboard.pause()
    settle(win)
    assert prompts.pauses == 1
    assert fake.script_running is True
    assert fake.released is False


def test_motor_starting_during_the_pause_prompt_refuses(gui):
    win, fake, prompts = gui()

    def spin_up():
        fake.erpm = 5000
        return True

    prompts.confirm_pause = spin_up
    win.dashboard.pause()
    settle(win)
    assert fake.script_running is True
    assert prompts.errors[-1][0] == "Refused"


def test_pause_refused_while_turning(gui):
    win, fake, prompts = gui()
    fake.erpm = 5000
    win.dashboard.pause()
    settle(win)
    assert prompts.errors[-1][0] == "Refused"
    assert "RC switch" in prompts.errors[-1][1]
    assert fake.script_running is True
    assert fake.released is False


def test_pause_releases_the_motor_and_warns(gui):
    win, fake, _ = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: not win.paused_banner.isHidden())
    assert fake.script_running is False
    assert fake.released is True


def test_start_needs_confirmation(gui):
    win, fake, prompts = gui()
    win.dashboard.pause()
    settle(win)
    prompts.start_answer = False
    win.dashboard.start()
    settle(win)
    assert prompts.starts == 1
    assert fake.script_running is False
    assert not win.paused_banner.isHidden()


def test_start_refused_while_turning_without_asking(gui):
    win, fake, prompts = gui()
    win.dashboard.pause()
    settle(win)
    fake.erpm = 5000
    win.dashboard.start()
    settle(win)
    assert prompts.starts == 0
    assert prompts.errors[-1][0] == "Refused"
    assert fake.script_running is False


def test_motor_starting_during_the_start_prompt_refuses(gui):
    win, fake, prompts = gui()
    win.dashboard.pause()
    settle(win)
    def spin_up():
        fake.erpm = 5000
        return True
    prompts.confirm_start = spin_up
    win.dashboard.start()
    settle(win)
    assert fake.script_running is False
    assert prompts.errors[-1][0] == "Refused"


# ---------------------------------------------------------------------- console
@pytest.mark.parametrize("text", ["(conf-store)", "(cmds-proc 73)", "(set-current 5)", "(eval 1)", ":pause"])
def test_repl_refuses_dangerous_words(gui, text):
    win, fake, prompts = gui()
    fake.commands.clear()
    win.console.repl_input.setText(text)
    win.console.send_repl()
    settle(win)
    wait_until(lambda: "Refused" in win.console.output.toPlainText())
    assert p.COMM_LISP_REPL_CMD not in fake.commands


def test_repl_runs_a_harmless_expression(gui):
    win, _, _ = gui()
    win.console.repl_input.setText("(+ 1 2)")
    win.console.send_repl()
    wait_until(lambda: "> (+ 1 2)" in win.console.output.toPlainText())


def test_repl_asks_before_resuming_a_paused_script(gui):
    win, fake, prompts = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: win.script_paused)
    prompts.repl_answer = False
    fake.commands.clear()
    win.console.repl_input.setText("(+ 1 2)")
    win.console.send_repl()
    settle(win)
    assert prompts.repl_asks == 1
    assert p.COMM_LISP_REPL_CMD not in fake.commands
    assert fake.script_running is False


def test_repl_expression_clears_the_paused_banner_when_it_resumes(gui):
    win, fake, _ = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: win.script_paused)
    win.console.repl_input.setText("(+ 1 2)")
    win.console.send_repl()
    wait_until(lambda: win.paused_banner.isHidden())
    assert fake.script_running is True


def test_repl_meta_command_does_not_ask(gui):
    win, _, prompts = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: win.script_paused)
    win.console.repl_input.setText(":info")
    win.console.send_repl()
    settle(win)
    assert prompts.repl_asks == 0


def test_terminal_offers_only_read_only_commands(gui):
    win, fake, _ = gui()
    from sky_programmer import safety

    choice = win.console.term_choice
    assert {choice.itemText(i) for i in range(choice.count())} == safety.TERMINAL_READ_ONLY
    choice.setCurrentText("faults")
    win.console.run_terminal()
    wait_until(lambda: "fault log empty" in win.console.output.toPlainText())


def test_board_messages_appear_in_the_console(gui):
    win, fake, _ = gui()
    fake._print(p.COMM_LISP_PRINT, "hello from the script")
    wait_until(lambda: "[board] hello from the script" in win.console.output.toPlainText())


# ---------------------------------------------------------------------- backups
def _script_changes(win, fake, **values):
    from sky_programmer import config

    fake.mc = config.patch_blob(fake.mc, win.layout, "mc_configuration", values)


def test_backing_up_over_drift_shows_what_it_would_accept_and_can_be_refused(gui):
    win, fake, prompts = gui()
    _script_changes(win, fake, l_current_max=20.0)
    before = win.backups_page.table.rowCount()
    prompts.baseline_answer = False
    win.backups_page.backup_now()
    settle(win)
    assert [row[0] for row in prompts.baseline_requests[0]] == ["l_current_max"]
    assert win.backups_page.table.rowCount() == before
    edit_and_review(win, l_temp_fet_start="84")
    assert fake.written == []
    assert prompts.errors[-1][0] == "Refused"


def test_a_change_during_the_backup_question_is_not_saved(gui):
    win, fake, prompts = gui()
    _script_changes(win, fake, l_current_max=20.0)
    before = win.backups_page.table.rowCount()

    def changes_again(rows):
        _script_changes(win, fake, l_max_erpm=33333.0)
        return True

    prompts.confirm_new_baseline = changes_again
    win.backups_page.backup_now()
    settle(win)
    wait_until(lambda: prompts.errors)
    assert prompts.errors[-1][0] == "Refused"
    assert win.backups_page.table.rowCount() == before


def test_backing_up_with_no_drift_does_not_ask(gui):
    win, _, prompts = gui()
    win.backups_page.backup_now()
    settle(win)
    assert prompts.baseline_requests == []


def test_a_backup_file_that_is_valid_json_but_wrong_cannot_break_connecting_or_listing(gui, tmp_path):
    win, _, prompts = gui(connect=False)
    folder = tmp_path / "backups" / "0102030405060708090a0b0c"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "99999999-000000-000000000-manual.json").write_text("[]")
    win.connect_board()
    wait_until(lambda: win.info is not None or win.board is None)
    settle(win)
    assert win.info is not None
    assert "Damaged" in win.backups_page.table.item(0, 1).text()


def test_a_failed_write_is_reported_even_if_refreshing_the_list_fails(gui, monkeypatch):
    win, fake, prompts = gui(write_mode="ignored")

    def broken():
        raise TypeError("list refresh failed")

    monkeypatch.setattr(win.backups_page, "refresh", broken)
    edit_and_review(win, l_temp_fet_start="84")
    wait_until(lambda: prompts.errors)
    assert prompts.errors[-1][0] == "The write did not verify"


def test_written_message_only_mentions_a_pause_if_there_was_one(gui):
    win, _, prompts = gui(script_present=False)
    edit_and_review(win, l_temp_fet_start="84")
    wait_until(lambda: prompts.infos)
    assert prompts.infos[-1][0] == "Written"
    assert "paused" not in prompts.infos[-1][1]


def test_backup_now_lists_it(gui):
    win, _, _ = gui(baseline=False)
    assert win.backups_page.table.rowCount() == 0
    win.backups_page.backup_now()
    settle(win)
    wait_until(lambda: win.backups_page.table.rowCount() == 1)


def test_restore_goes_through_the_same_path(gui):
    win, fake, prompts = gui()
    edit_and_review(win, l_temp_fet_start="84")
    page = win.backups_page
    wait_until(lambda: page.table.rowCount() >= 3)
    page.table.selectRow(page.table.rowCount() - 1)  # the oldest: the baseline
    page.restore()
    settle(win)
    wait_until(lambda: fake.value("l_temp_fet_start") == 85)
    assert len(prompts.write_requests) == 2


def test_compare_shows_differences(gui):
    win, _, prompts = gui()
    edit_and_review(win, l_temp_fet_start="84")
    page = win.backups_page
    wait_until(lambda: page.table.rowCount() >= 3)
    page.table.selectRow(page.table.rowCount() - 1)
    page.compare()
    settle(win)
    wait_until(lambda: prompts.differences)
    assert prompts.differences[-1][0][0] == "l_temp_fet_start"


def test_a_damaged_backup_is_shown_and_cannot_be_restored(gui):
    win, fake, prompts = gui()
    folder = Path(win.backup_dir) / win.info["ident"]["uuid"]
    (folder / "99999999-000000-000000000-manual.json").write_text("{not json")
    page = win.backups_page
    page.refresh()
    assert "Damaged" in page.table.item(0, 1).text()
    page.table.selectRow(0)
    page.restore()
    settle(win)
    assert fake.written == []
    assert prompts.errors


# ---------------------------------------------------------------------- closing
def test_closing_is_refused_during_a_write(gui):
    win, _, prompts = gui()
    gate = threading.Event()
    win.board.submit(lambda prog: gate.wait(10), exclusive=True)
    assert win.close() is False
    assert win.isVisible()
    assert prompts.infos[-1][0] == "Please wait"
    gate.set()
    settle(win)


def test_disconnect_is_blocked_during_a_write(gui):
    win, _, _ = gui()
    gate = threading.Event()
    win.board.submit(lambda prog: gate.wait(10), exclusive=True)
    QApplication.processEvents()
    assert not win.connect_button.isEnabled()
    board = win.board
    win.disconnect_board()
    assert win.board is board
    gate.set()
    settle(win)


def test_closing_with_a_paused_script_asks_first(gui):
    win, _, prompts = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: win.script_paused)
    prompts.close_answer = False
    assert win.close() is False
    assert win.isVisible()


def test_reconnecting_remembers_this_apps_pause(gui):
    win, fake, prompts = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: win.script_paused)
    win.disconnect_board()
    wait_until(lambda: win.board is None)
    win.connect_board()
    wait_until(lambda: win.info is not None)
    settle(win)
    wait_until(lambda: not win.paused_banner.isHidden())
    assert win.script_paused
    win.console.repl_input.setText("(+ 1 2)")
    prompts.repl_answer = False
    win.console.send_repl()
    settle(win)
    assert prompts.repl_asks == 1
    assert fake.script_running is False


def test_operator_can_say_the_board_was_power_cycled(gui):
    win, _, _ = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: win.script_paused)
    win.forget_pause()
    settle(win)
    wait_until(lambda: win.paused_banner.isHidden())
    win.disconnect_board()
    wait_until(lambda: win.board is None)
    win.connect_board()
    wait_until(lambda: win.info is not None)
    settle(win)
    assert win.paused_banner.isHidden()


def test_saying_it_was_power_cycled_needs_a_yes(gui):
    win, _, prompts = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: win.script_paused)
    prompts.power_cycled_answer = False
    win.forget_pause()
    settle(win)
    assert win.script_paused


def test_the_banner_buttons_are_disabled_while_busy(gui):
    win, _, _ = gui()
    gate = threading.Event()
    win.board.submit(lambda prog: gate.wait(10), exclusive=True)
    QApplication.processEvents()
    assert not any(b.isEnabled() for b in win.paused_banner_buttons)
    gate.set()
    settle(win)
    assert all(b.isEnabled() for b in win.paused_banner_buttons)


def test_a_restart_remembers_this_apps_pause(gui, tmp_path):
    win, fake, _ = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: win.script_paused)
    win.disconnect_board()
    wait_until(lambda: win.board is None)
    win.settings.sync()
    prompts = FakePrompts()
    again = MainWindow(link_factory=win.link_factory, ports=lambda: ["/dev/ttyFAKE"], backup_dir=win.backup_dir,
                       prompts=prompts, settings_path=tmp_path / "settings.ini", repo_dir=tmp_path / "no-repo")
    again.show()
    again.connect_board()
    wait_until(lambda: again.script_paused)
    assert not again.paused_banner.isHidden()
    wait_until(lambda: not again.board.busy)
    again.close()


def test_repl_resume_is_refused_while_the_motor_turns(gui):
    win, fake, _ = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: win.script_paused)
    fake.erpm = 5000
    fake.commands.clear()
    win.console.repl_input.setText("(+ 1 2)")
    win.console.send_repl()
    settle(win)
    wait_until(lambda: "turning" in win.console.output.toPlainText())
    assert p.COMM_LISP_REPL_CMD not in fake.commands
    assert fake.script_running is False


def test_a_lost_pause_reply_still_shows_the_banner(gui):
    win, fake, prompts = gui()
    fake.lose_running_reply = True
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: prompts.errors)
    wait_until(lambda: not win.paused_banner.isHidden())
    assert "may" in win.paused_banner.text.text()


def test_disconnecting_with_a_paused_script_warns(gui):
    win, _, prompts = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: win.script_paused)
    win.disconnect_board()
    wait_until(lambda: win.board is None)
    assert prompts.infos[-1][0] == "The script may still be paused"
    assert win.paused_banner.isHidden()


def test_nothing_can_be_queued_on_a_board_that_is_gone(gui):
    win, fake, _ = gui()
    board = win.board
    fake.unplugged = True
    wait_until(lambda: win.board is None)
    assert board.submit(lambda prog: None) is False


def test_board_lost_during_the_review_dialog_writes_nothing_and_says_so(gui):
    win, fake, prompts = gui()

    def unplug_during_dialog(rows, warnings, target=""):
        fake.unplugged = True
        wait_until(lambda: win.board is None)
        return True

    prompts.confirm_write = unplug_during_dialog
    edit_and_review(win, l_temp_fet_start="84")
    wait_until(lambda: any(title == "Not written" for title, _ in prompts.errors))
    assert fake.written == []


def test_board_lost_during_the_start_prompt_starts_nothing_and_says_so(gui):
    win, fake, prompts = gui()
    win.dashboard.pause()
    settle(win)

    def unplug_during_prompt():
        fake.unplugged = True
        wait_until(lambda: win.board is None)
        return True

    prompts.confirm_start = unplug_during_prompt
    win.dashboard.start()
    wait_until(lambda: any(title == "Not started" for title, _ in prompts.errors))
    assert fake.script_running is False


# ---------------------------------------------------------------------- recording
def test_csv_recording_uses_the_terminal_columns(gui, tmp_path):
    win, _, prompts = gui()
    prompts.csv_path = str(tmp_path / "run.csv")
    win.dashboard.toggle_recording()
    count = win.dashboard.charts["v_in"].points
    start = len(count)
    wait_until(lambda: len(win.dashboard.charts["v_in"].points) >= start + 3)
    win.dashboard.toggle_recording()
    lines = (tmp_path / "run.csv").read_text().splitlines()
    assert lines[0].split(",") == ["t", "erpm", "duty", "motor_a", "input_a", "bus_v", "fet_c", "motor_c", "fault"]
    assert len(lines) >= 3
    assert lines[1].split(",")[5] == "48.0"
    assert lines[1].split(",")[8] == "FAULT_CODE_NONE"


def test_recording_stops_and_closes_on_disconnect(gui, tmp_path):
    win, _, prompts = gui()
    prompts.csv_path = str(tmp_path / "run.csv")
    win.dashboard.toggle_recording()
    handle = win.dashboard._csv_file
    win.disconnect_board()
    wait_until(lambda: win.board is None)
    assert handle.closed
    assert win.dashboard._csv is None


def test_a_full_disk_stops_recording_and_says_so(gui, tmp_path):
    win, _, prompts = gui()
    prompts.csv_path = "/dev/full"
    win.dashboard.toggle_recording()
    wait_until(lambda: any(title == "Recording stopped" for title, _ in prompts.errors))
    assert win.dashboard._csv is None
    assert win.dashboard.record_button.text() == "Record CSV…"


def test_failing_to_close_the_port_still_disconnects(gui):
    win, fake, prompts = gui()
    board_link_close = fake.close

    def raising_close():
        board_link_close()
        raise OSError("device vanished")

    fake.close = raising_close
    win.disconnect_board()
    wait_until(lambda: win.board is None)
    assert win.connect_button.text() == "Connect"


def test_unwritable_csv_path_is_reported(gui, tmp_path):
    win, _, prompts = gui()
    prompts.csv_path = str(tmp_path / "missing" / "run.csv")
    win.dashboard.toggle_recording()
    assert prompts.errors[-1][0] == "Could not record"


# ---------------------------------------------------------------------- day and night
def _luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("palette", [theme.DAY, theme.NIGHT], ids=["day", "night"])
@pytest.mark.parametrize("text, background", [
    ("ink", "surface"), ("ink", "background"), ("ink", "surface_alt"), ("ink", "changed"), ("ink", "invalid"),
    ("ink", "locked"), ("muted", "surface"), ("muted", "background"), ("muted", "surface_alt"),
    ("on_primary", "primary"), ("accent_ink", "accent_bg"), ("good", "surface"), ("critical", "surface"),
    ("warning_ink", "warning_bg"), ("demo_ink", "demo_bg"), ("ink", "info_bg"),
])
def test_text_is_readable_in_both_themes(palette, text, background):
    ratio = _contrast(getattr(palette, text), getattr(palette, background))
    assert ratio >= 4.5, f"{palette.name}: {text} on {background} is only {ratio:.2f}:1"


@pytest.mark.parametrize("palette", [theme.DAY, theme.NIGHT], ids=["day", "night"])
def test_the_chart_line_is_visible_in_both_themes(palette):
    assert _contrast(palette.brand, palette.surface) >= 3


def test_every_stylesheet_image_exists():
    import re

    for palette in (theme.DAY, theme.NIGHT):
        for path in re.findall(r'url\("([^"]+)"\)', theme.qss(palette)):
            assert Path(path).is_file(), path


def test_the_theme_toggle_switches_and_is_remembered(gui, tmp_path):
    win, _, _ = gui()
    win.set_theme("day")
    assert theme.current is theme.DAY
    assert "Day" in win.theme_button.text()
    win.toggle_theme()
    assert theme.current is theme.NIGHT
    assert "Night" in win.theme_button.text()
    assert QApplication.instance().styleSheet() == theme.qss(theme.NIGHT)
    win.settings.sync()
    assert "theme=night" in (tmp_path / "settings.ini").read_text()
    other = MainWindow(ports=lambda: [], backup_dir=tmp_path, prompts=FakePrompts(),
                       settings_path=tmp_path / "settings.ini", repo_dir=tmp_path / "no-repo")
    assert other.theme_name == "night"
    other.close()
    win.set_theme("day")


def test_an_edited_dropdown_row_is_highlighted_all_the_way_across(gui):
    win, _, _ = gui()
    win.set_theme("day")
    table = win.config_page.motor
    table.set_value("motor_type", "MOTOR_TYPE_FOC")
    assert table.item(table.rows["motor_type"], VALUE).background().color().name().upper() == theme.DAY.changed
    assert table.text_of("motor_type") == "MOTOR_TYPE_FOC"


def test_switching_theme_keeps_edits_and_recolours_them(gui):
    win, _, _ = gui()
    win.set_theme("day")
    table = win.config_page.motor
    table.set_value("l_temp_fet_start", "84")
    row = table.rows["l_temp_fet_start"]
    assert table.item(row, VALUE).background().color().name().upper() == theme.DAY.changed
    win.set_theme("night")
    assert table.staged == {"l_temp_fet_start": "84"}
    assert table.item(row, VALUE).background().color().name().upper() == theme.NIGHT.changed
    win.set_theme("day")


# ---------------------------------------------------------------------- updates
@pytest.fixture
def source_folder(tmp_path, monkeypatch):
    """(checkout, other clone): a throwaway project with a local remote. Never this repo, never the network."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    def git(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

    bare, seed, work, other = (tmp_path / n for n in ("remote.git", "seed", "work", "other"))
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "src" / "sky_programmer").mkdir(parents=True)
    (seed / "src" / "sky_programmer" / "__init__.py").write_text("")
    (seed / "f.txt").write_text("one\n")
    for cfg in (("user.email", "t@t"), ("user.name", "t")):
        git(seed, "config", *cfg)
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "one")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "-u", "origin", "main")
    for clone in (work, other):
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        for cfg in (("user.email", "t@t"), ("user.name", "t")):
            git(clone, "config", *cfg)
    (other / "new.txt").write_text("new\n")
    git(other, "add", ".")
    git(other, "commit", "-qm", "from github")
    git(other, "push", "-q")
    return work


def _updates(win, folder):
    page = win.updates_page
    page.set_repo(folder)
    return page


def test_updates_is_open_with_no_board_and_board_pages_are_not(gui):
    win, _, _ = gui(connect=False)
    assert win.nav.item(4).flags() & Qt.ItemFlag.ItemIsEnabled
    assert not win.nav.item(0).flags() & Qt.ItemFlag.ItemIsEnabled
    win.nav.setCurrentRow(4)
    assert win.stack.currentWidget() is win.updates_page


def test_status_shows_the_folder(gui, source_folder):
    win, _, _ = gui(connect=False)
    page = _updates(win, source_folder)
    page.refresh()
    wait_until(lambda: not page.busy)
    assert "branch     main @" in page.output.toPlainText()


def test_pull_updates_the_folder_and_says_this_copy_is_not_changed(gui, source_folder):
    win, _, prompts = gui(connect=False)
    page = _updates(win, source_folder)
    page.pull()
    wait_until(lambda: not page.busy)
    assert (source_folder / "new.txt").exists()
    assert prompts.infos[-1][0] == "Folder updated"
    assert not win.code_stale


def test_when_the_app_runs_from_the_folder_a_pull_forces_a_restart_before_connecting(gui, source_folder, monkeypatch):
    monkeypatch.setattr("sky_programmer.gitops.running_from", lambda repo: True)
    win, fake, prompts = gui(connect=False)
    page = _updates(win, source_folder)
    page.pull()
    wait_until(lambda: not page.busy)
    assert win.code_stale
    assert not win.stale_banner.isHidden()
    win.connect_board()
    assert win.board is None
    assert prompts.infos[-1][0] == "Restart first"
    assert fake.commands == []


def test_connecting_is_refused_while_an_update_runs(gui, source_folder):
    win, _, prompts = gui(connect=False)
    page = _updates(win, source_folder)
    gate = threading.Event()
    page._start(lambda: gate.wait(10), lambda _: None)
    QApplication.processEvents()
    assert not win.connect_button.isEnabled()
    win.connect_board()
    assert win.board is None
    assert prompts.infos[-1][0] == "Please wait"
    gate.set()
    wait_until(lambda: not page.busy)
    assert win.connect_button.isEnabled()


def test_the_connect_button_recovers_if_the_board_goes_during_a_status_job(gui, source_folder):
    win, fake, _ = gui()
    page = _updates(win, source_folder)
    gate = threading.Event()
    page._start(lambda: gate.wait(10), lambda _: None)
    fake.unplugged = True
    wait_until(lambda: win.board is None)
    gate.set()
    wait_until(lambda: not page.busy)
    assert win.connect_button.isEnabled()
    assert win.connect_button.text() == "Connect"


def test_the_simulated_board_says_updates_act_on_the_real_folder(tmp_path, source_folder):
    QApplication.instance() or QApplication([])
    win = MainWindow(backup_dir=tmp_path, demo=DemoBoard(), prompts=FakePrompts(),
                     settings_path=tmp_path / "settings.ini", repo_dir=source_folder)
    win.show()
    wait_until(lambda: win.info is not None)
    assert "real folder" in win.updates_page.hint.text()
    wait_until(lambda: not win.board.busy)
    win.close()


def test_pull_and_reset_are_refused_while_a_board_is_connected(gui, source_folder):
    win, _, prompts = gui()
    page = _updates(win, source_folder)
    win._sync()
    assert not page.pull_button.isEnabled()
    assert not page.reset_button.isEnabled()
    assert page.status_button.isEnabled()
    page.pull()
    page.reset_folder()
    assert prompts.infos[-1][0] == "Disconnect first"
    assert not page.busy
    assert not (source_folder / "new.txt").exists()


def test_reset_is_cancelled_unless_confirmed(gui, source_folder):
    win, _, prompts = gui(connect=False)
    page = _updates(win, source_folder)
    (source_folder / "f.txt").write_text("my work\n")
    prompts.reset_answer = False
    page.reset_folder()
    wait_until(lambda: prompts.resets and not page.busy)
    assert any("f.txt" in line for line in prompts.resets[0]["lost_files"])
    assert (source_folder / "f.txt").read_text() == "my work\n"


def test_reset_makes_the_folder_match_github(gui, source_folder):
    win, _, prompts = gui(connect=False)
    page = _updates(win, source_folder)
    (source_folder / "f.txt").write_text("my work\n")
    page.reset_folder()
    wait_until(lambda: prompts.resets and not page.busy)
    assert (source_folder / "f.txt").read_text() == "one\n"
    assert (source_folder / "new.txt").exists()
    assert "To go back" in page.output.toPlainText()


def test_connecting_during_the_reset_dialog_stops_the_reset(gui, source_folder):
    win, _, prompts = gui(connect=False)
    page = _updates(win, source_folder)
    (source_folder / "f.txt").write_text("my work\n")

    def connect_during_dialog(preview):
        win.connect_board()
        wait_until(lambda: win.info is not None)
        return True

    prompts.confirm_reset = connect_during_dialog
    page.reset_folder()
    wait_until(lambda: any(title == "Disconnect first" for title, _ in prompts.infos))
    assert (source_folder / "f.txt").read_text() == "my work\n"


def test_disconnecting_never_touches_the_source_folder(gui, source_folder):
    win, _, prompts = gui()
    page = _updates(win, source_folder)
    (source_folder / "f.txt").write_text("my work\n")
    win.disconnect_board()
    wait_until(lambda: win.board is None)
    assert prompts.resets == []
    assert not page.busy
    assert (source_folder / "f.txt").read_text() == "my work\n"


def test_a_folder_that_is_not_this_project_cannot_be_chosen(gui, tmp_path):
    win, _, prompts = gui(connect=False)
    other = tmp_path / "someone-elses"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    prompts.folder = str(other)
    win.updates_page.choose_folder()
    assert prompts.errors[-1][0] == "Not this project"
    assert not win.updates_page.pull_button.isEnabled()


def test_closing_is_refused_during_an_update(gui, source_folder):
    win, _, prompts = gui(connect=False)
    page = _updates(win, source_folder)
    gate = threading.Event()
    page._start(lambda: gate.wait(10), lambda _: None)
    assert win.close() is False
    assert prompts.infos[-1][0] == "Please wait"
    gate.set()
    wait_until(lambda: not page.busy)


def test_git_failing_to_run_is_shown_on_the_page(gui, source_folder, monkeypatch):
    win, _, _ = gui(connect=False)
    page = _updates(win, source_folder)

    def broken(repo):
        raise FileNotFoundError("git")

    monkeypatch.setattr("sky_programmer.gitops.pull", broken)
    page.pull()
    wait_until(lambda: not page.busy)
    assert "Could not run git" in page.output.toPlainText()


# ---------------------------------------------------------------------- safety boundaries in the source
def _gui_sources():
    """demo.py is left out: it plays the board's side of the link, so it speaks in packets."""
    folder = Path(resources.files("sky_programmer.gui"))
    return {path.name: path.read_text() for path in folder.glob("*.py") if path.name != "demo.py"}


@pytest.mark.parametrize("word", ["allow_risky", "accept_drift", "allow_default_foc", "allow_other_board",
                                  "allow_motor", "force=True", "config_write", "COMM_", "check_packet",
                                  "safety.MOTOR", "cmds-proc"])
def test_the_gui_has_no_override_or_raw_packet_path(word):
    for name, text in _gui_sources().items():
        assert word not in text, f"{name} contains {word}"


def test_only_the_link_layer_opens_serial():
    for name, text in _gui_sources().items():
        assert "import serial" not in text, name
        assert ".ser." not in text, name


def test_only_the_board_thread_builds_a_programmer():
    users = [name for name, text in _gui_sources().items() if "Programmer(" in text]
    assert users == ["board.py"]


def test_only_write_flow_commits():
    users = [name for name, text in _gui_sources().items() if ".commit(" in text]
    assert users == ["window.py"]


# ---------------------------------------------------------------------- dialogs and small pieces
def test_typed_confirm_needs_exactly_yes():
    QApplication.instance() or QApplication([])
    d = TypedConfirm(None, "t", "body", "prompt", "Write", [("a", "1", "2")], ["careful"])
    assert not d.go.isEnabled()
    for wrong in ("yes", "Y", "YES!", ""):
        d.answer.setText(wrong)
        assert not d.go.isEnabled(), wrong
    d.answer.setText(" YES ")
    assert d.go.isEnabled()
    assert not d.go.isDefault()


def test_typed_confirm_enter_only_works_once_the_word_is_right():
    QApplication.instance() or QApplication([])
    d = TypedConfirm(None, "t", "", "prompt", "Write")
    d.answer.setText("nope")
    d.answer.returnPressed.emit()
    assert d.result() != d.DialogCode.Accepted
    d.answer.setText("YES")
    d.answer.returnPressed.emit()
    assert d.result() == d.DialogCode.Accepted
    d2 = TypedConfirm(None, "t", "", "prompt", "Reset", word="RESET")
    d2.answer.setText("YES")
    assert not d2.go.isEnabled()
    d2.answer.setText("RESET")
    assert d2.go.isEnabled()


def test_dead_board_wording_reads_properly():
    out = humanize("Reconnect and run `sky status`, then `sky diff <backup>`.")
    assert out == "Reconnect, then Compare on the Backups page."


@pytest.mark.parametrize("text, expect", [
    ("Run `sky backup` first", "Back up now"),
    ("Usually a running script: run `sky stop` first.", "Pause script"),
    ("run `sky start` or power-cycle", "Start script"),
    ("Run `sky diff /x/y.json`, then `sky restore /x/y.json` after", "Compare on the Backups page"),
])
def test_humanize_names_the_buttons(text, expect):
    out = humanize(text)
    assert expect in out and "`sky " not in out


def test_humanize_is_honest_about_flags():
    assert NO_OVERRIDE in humanize("Pass --allow-risky if you mean it.")
    assert NO_OVERRIDE not in humanize("The motor is 3-4 A")


def test_logo_loads_and_the_icon_is_square():
    QApplication.instance() or QApplication([])
    assert not theme.logo_pixmap(36).isNull()
    icon = theme.icon_image(256)
    assert (icon.width(), icon.height()) == (256, 256)


def test_launcher_is_written_for_this_user_only(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    entry = install_launcher()
    assert entry == tmp_path / "applications" / "sky-programmer.desktop"
    text = entry.read_text()
    assert "Exec=" in text and "Icon=sky-programmer" in text and "Terminal=false" in text
    assert (tmp_path / "icons/hicolor/256x256/apps/sky-programmer.png").stat().st_size > 1000


def test_simulated_backups_cannot_reach_the_real_folder(tmp_path):
    QApplication.instance() or QApplication([])
    win = MainWindow(backup_dir=tmp_path, demo=DemoBoard(), prompts=FakePrompts(),
                     settings_path=tmp_path / "settings.ini", repo_dir=tmp_path / "no-repo")
    win.show()
    wait_until(lambda: win.info is not None)
    assert win.backup_dir == str(tmp_path)
    assert str(backup.default_dir()) != win.backup_dir
    assert not win.demo_banner.isHidden()
    win.close()
