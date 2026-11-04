"""The rules that keep the board alive. Changing one of these should be a deliberate decision."""
import ast
from pathlib import Path

import pytest

from sky_programmer import protocol as p
from sky_programmer import safety as s
from sky_programmer.errors import SafetyRefusal
from sky_programmer.link import Link

SRC = Path(__file__).resolve().parent.parent / "src" / "sky_programmer"

BRICKING = {
    "JUMP_TO_BOOTLOADER": 1, "ERASE_NEW_APP": 2, "WRITE_NEW_APP_DATA": 3, "FORWARD_CAN": 34,
    "JUMP_TO_BOOTLOADER_ALL_CAN": 59, "ERASE_NEW_APP_ALL_CAN": 60, "WRITE_NEW_APP_DATA_ALL_CAN": 61,
    "BM_ERASE_FLASH_ALL": 67, "BM_WRITE_FLASH": 68, "ERASE_BOOTLOADER": 73, "ERASE_BOOTLOADER_ALL_CAN": 74,
    "WRITE_NEW_APP_DATA_LZO": 81, "WRITE_NEW_APP_DATA_ALL_CAN_LZO": 82, "JUMP_TO_BOOTLOADER_HW": 102,
    "ERASE_NEW_APP_HW": 103, "WRITE_NEW_APP_DATA_HW": 104, "ERASE_BOOTLOADER_HW": 105,
}  # fmt: skip


@pytest.mark.parametrize("name,cmd", BRICKING.items())
def test_every_flash_command_is_refused_even_with_write_permission(name, cmd):
    for payload in (bytes([cmd]), bytes([cmd, 0, 0, 0, 0, 1, 2, 3])):
        with pytest.raises(SafetyRefusal):
            s.check_packet(payload, config_write=True, repl_allow_motor=True)


def test_allow_list_is_exactly_what_was_reviewed():
    assert set(s.ALLOWED) == {0, 4, 13, 14, 15, 17, 20, 133, 138, 159}


def test_every_other_command_number_is_refused():
    for cmd in set(range(256)) - set(s.ALLOWED):
        with pytest.raises(SafetyRefusal):
            s.check_packet(bytes([cmd]), config_write=True)


def test_allowed_and_forbidden_do_not_overlap():
    assert not set(s.ALLOWED) & set(s.FORBIDDEN)


def test_blocked_packet_never_reaches_the_port():
    class Port:
        sent = b""

        def write(self, data):
            Port.sent += data

    with pytest.raises(SafetyRefusal):
        Link(Port()).send(bytes([2, 0, 0, 0, 0]))
    assert Port.sent == b""


def test_config_write_needs_the_write_path():
    with pytest.raises(SafetyRefusal):
        s.check_packet(bytes([p.COMM_SET_MCCONF, 1, 2, 3]))


@pytest.mark.parametrize("payload", [bytes([159]), bytes([159, 0xFF, 0xFF]), bytes([159, 3, 232, 0]),
                                     bytes([6, 0, 0, 0, 0])])
def test_only_the_fixed_release_packet(payload):
    with pytest.raises(SafetyRefusal):
        s.check_packet(payload)
    s.check_packet(s.MOTOR_RELEASE_PACKET)


def test_read_commands_carry_no_data():
    with pytest.raises(SafetyRefusal):
        s.check_packet(bytes([p.COMM_GET_MCCONF, 1]))


@pytest.mark.parametrize("cmd", ["foc_openloop 10 100", "foc_detect_apply_all 40", "rebootwdt", "measure_res 5",
                                 "faults extra", "param_detect 1 2 3"])  # fmt: skip
def test_terminal_refuses_anything_that_acts(cmd):
    with pytest.raises(SafetyRefusal):
        s.check_terminal(cmd)


@pytest.mark.parametrize("cmd", ["faults", "fw_info", "hw_status", "mem"])
def test_terminal_allows_reports(cmd):
    s.check_terminal(cmd)


@pytest.mark.parametrize("line", [
    "(conf-store)", "( conf-store )", "(CONF-STORE)", "(conf-restore-mc)", "(load-native-lib x)", "(shutdown)",
    "(can-cmd 1 \"(conf-store)\")", "(fw-erase 100)", "(bm-connect)", "(read-eval \"(+ 1 1)\")",
    "(if t (conf-store) 0)", "(eval (read \"(conf-store)\"))", ":reset", ":RESET now", ":continue", "  :reset",
    ":pause", ":env", ":symbols", ":undef x", ":prof start",
    "(cmds-start-stop 1)", "(cmds-proc [2 1 73 217 237 3])", "(can-send-eid 1 [1])", "(can-send-sid 1 [1])",
    "(image-save)", "(eeprom-store-i 0 1)", "(eeprom-store-f 0 1.0)", "(can-update-baud 500)", "(conf-store;c\\n)",
    "(conf-store;x)", "(gpio-configure 'pin-swdio 'pin-mode-out)", "(canset-current 1 50)", "(set-odometer 0)",
    "(move-to-flash x)", "@const-start", "(str-merge \"cmds\" \"-proc\")(read x)", "{conf-store}", "(bm-connect)",
    "(print \"a\\x\")",
])  # fmt: skip
def test_repl_blocks_flash_and_restart(line):
    with pytest.raises(SafetyRefusal):
        s.check_repl(line, allow_motor=True)


@pytest.mark.parametrize("line", ["(set-current 50)", "(set-duty 0.5)", "(foc-openloop 10 100)",
                                  "(conf-set 'l-current-max 100)", "(set-servo 0.5)", "(conf-detect-foc 40 40 1)",
                                  "(pwm-start 240 0.1)", "(gpio-write 1 1)", "(override-temp-motor 20)",
                                  "(app-disable-output 1000)"])
def test_repl_motor_words_need_the_flag(line):
    with pytest.raises(SafetyRefusal):
        s.check_repl(line)
    s.check_repl(line, allow_motor=True)


@pytest.mark.parametrize("line", ["(get-adc 0)", "(+ 1 2)", ":info", ":ctxs", "(ext-hall-rpm)", "(get-rpm)",
                                  "(throttle-goto-deg 10)", "(conf-get 'l-current-max)", "(print (get-vin))"])
def test_repl_allows_normal_reading(line):
    s.check_repl(line)


def test_repl_refuses_input_the_firmware_would_truncate():
    with pytest.raises(SafetyRefusal):
        s.check_repl("(+ 1 " + "1 " * 200 + ")")


def _sources():
    return {f.name: ast.parse(f.read_text()) for f in SRC.glob("*.py")}


def test_only_the_link_writes_to_the_port_and_it_checks_first():
    for name, tree in _sources().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "write" \
                    and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "ser":
                assert name == "link.py"
    src = (SRC / "link.py").read_text()
    body = src[src.index("def send("):src.index("def _pump")]
    assert body.index("check_packet(") < body.index("self.ser.write(")


def test_write_permission_is_granted_in_one_place_only():
    grants = []
    for name, tree in _sources().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "config_write" \
                    and isinstance(node.value, ast.Constant) and node.value.value is True:
                grants.append(name)
    assert grants == ["programmer.py"]


def test_flash_refusal_says_it_is_permanent_and_why():
    with pytest.raises(SafetyRefusal, match=r"ERASE_BOOTLOADER \(73\) is permanently blocked"):
        s.check_packet(bytes([73]))


def test_every_firmware_word_is_classified():
    """A word added to the list must land in one known group, and what isn't read-only must stay blocked."""
    words = s.firmware_words()
    assert len(words) > 200
    for w in words:
        line = f"({w})"
        if s._repl_word_is_read_only(w):
            s.check_repl(line)
        else:
            with pytest.raises(SafetyRefusal):
                s.check_repl(line)
    assert "cmds-proc" in words and not s._repl_word_is_read_only("cmds-proc")


@pytest.mark.parametrize("word", ["set-current", "conf-store", "cmds-proc", "image-save", "eeprom-store-i", "shutdown",
                                  "can-send-eid", "gpio-configure", "set-odometer", "uart-write"])
def test_read_list_does_not_contain_acting_words(word):
    assert not s._repl_word_is_read_only(word)
