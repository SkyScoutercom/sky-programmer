"""The desktop app against the simulated board: the real window, the real worker thread, no real dialogs."""
import os
import threading
import time
from importlib import resources
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from sky_programmer import backup  # noqa: E402
from sky_programmer import protocol as p
from sky_programmer.gui import theme  # noqa: E402
from sky_programmer.gui.app import install_launcher  # noqa: E402
from sky_programmer.gui.config_page import VALUE, NoWheelCombo  # noqa: E402
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
        self.starts, self.repl_asks = 0, 0

    def confirm_write(self, rows, warnings):
        self.write_requests.append((rows, warnings))
        return self.write_answer

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
                         ports=lambda: ["/dev/ttyFAKE"], backup_dir=tmp_path / "backups", prompts=prompts)
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
    def spin_up(rows, warnings):
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
    def someone_else_writes(rows, warnings):
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


def test_disconnecting_with_a_paused_script_warns(gui):
    win, _, prompts = gui()
    win.dashboard.pause()
    settle(win)
    wait_until(lambda: win.script_paused)
    win.disconnect_board()
    wait_until(lambda: win.board is None)
    assert prompts.infos[-1][0] == "The script is still paused"
    assert win.paused_banner.isHidden()


def test_nothing_can_be_queued_on_a_board_that_is_gone(gui):
    win, fake, _ = gui()
    board = win.board
    fake.unplugged = True
    wait_until(lambda: win.board is None)
    assert board.submit(lambda prog: None) is False


def test_board_lost_during_the_review_dialog_writes_nothing_and_says_so(gui):
    win, fake, prompts = gui()

    def unplug_during_dialog(rows, warnings):
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


def test_unwritable_csv_path_is_reported(gui, tmp_path):
    win, _, prompts = gui()
    prompts.csv_path = str(tmp_path / "missing" / "run.csv")
    win.dashboard.toggle_recording()
    assert prompts.errors[-1][0] == "Could not record"


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
    win = MainWindow(backup_dir=tmp_path, demo=DemoBoard(), prompts=FakePrompts())
    win.show()
    wait_until(lambda: win.info is not None)
    assert win.backup_dir == str(tmp_path)
    assert str(backup.default_dir()) != win.backup_dir
    assert not win.demo_banner.isHidden()
    win.close()
