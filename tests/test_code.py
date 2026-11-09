"""Stage 2 step 1: backing up, trying and storing Lisp scripts, against the simulated firmware 7.00 code area."""
import json
import struct

import pytest

from sky_programmer import code
from sky_programmer import protocol as p
from sky_programmer.errors import CommsError, SafetyRefusal, VerifyFailed
from sky_programmer.safety import check_packet
from sky_programmer.simulator import STORED_SCRIPT

SCRIPT = '(print "hello from the new script")\n'


def big_script(size):
    line = "(define x 1) ; padding\n"
    return line * (size // len(line) + 1)


# ---------------------------------------------------------------------- packing and checking
def test_pack_and_image_match_the_firmware_format():
    content = code.pack(SCRIPT)
    assert content == SCRIPT.encode() + b"\0\0\0"
    img = code.image(content)
    n, crc, flags = struct.unpack_from(">IHH", img, 0)
    assert n == len(content) and flags == 0 and img[8:] == content
    assert crc == p.crc16(b"\0\0" + content)  # the firmware's check covers the flags too
    assert code.import_count(content) == 0 and code.script_text(content) == SCRIPT


def test_pack_refuses_a_script_too_big_for_the_area():
    code.pack("x" * (code.MAX_CONTENT_BYTES - 3))
    with pytest.raises(SafetyRefusal, match="at most"):
        code.pack("x" * (code.MAX_CONTENT_BYTES - 2))


def test_symbols_ignore_comments_strings_and_characters():
    text = '; (image-save)\n(print "fw-erase import") (define c \\#i) (Conf-Store)'
    assert code.symbols(text) == ["print", "define", "c", "conf-store"]


@pytest.mark.parametrize("word", ["image-save", "fw-erase", "cmds-proc", "import", "load-native-lib", "lbm-write"])
def test_forbidden_words_are_refused(word):
    with pytest.raises(SafetyRefusal, match=word):
        code.check_script(f"(defun f () ({word} 1))")


def test_warning_words_are_allowed_with_a_warning():
    warnings = code.check_script("(conf-store)")
    assert len(warnings) == 1 and "conf-store" in warnings[0]
    assert code.check_script(SCRIPT) == []


def test_a_nul_in_the_script_is_refused():
    with pytest.raises(SafetyRefusal, match="NUL"):
        code.check_script("(print 1)\0(print 2)")


# ---------------------------------------------------------------------- packet checks
def write_pkt(offset, data=b"\0" * 4):
    return bytes([p.COMM_LISP_WRITE_CODE]) + struct.pack(">I", offset) + data


def test_code_writes_need_the_upload_path():
    for pkt in (write_pkt(0), bytes([p.COMM_LISP_ERASE_CODE]) + struct.pack(">i", 100)):
        with pytest.raises(SafetyRefusal):
            check_packet(pkt)
        check_packet(pkt, code_write=True)


@pytest.mark.parametrize("pkt", [
    write_pkt(0, b""),  # writes nothing
    bytes([p.COMM_LISP_WRITE_CODE, 0, 0, 0]),  # under 4 bytes after the ID: the firmware's length underflows
    bytes([p.COMM_LISP_WRITE_CODE]),
    write_pkt(code.CODE_AREA_BYTES - 3),  # one byte past the end: the bootloader sector follows
    write_pkt(code.CODE_AREA_BYTES),
    write_pkt(0xFFFFFFFF),
    write_pkt(0x7FFFFFFF),
    bytes([p.COMM_LISP_ERASE_CODE]),
    bytes([p.COMM_LISP_ERASE_CODE]) + struct.pack(">i", 100) + b"\0",
])  # fmt: skip
def test_code_write_bounds(pkt):
    with pytest.raises(SafetyRefusal):
        check_packet(pkt, code_write=True)


def test_a_write_exactly_to_the_end_is_allowed():
    check_packet(write_pkt(code.CODE_AREA_BYTES - 4), code_write=True)
    check_packet(write_pkt(code.CODE_AREA_BYTES - 1, b"\0"), code_write=True)


@pytest.mark.parametrize("length,offset,ok", [
    (1, 0, True), (400, 0, True), (0, 0, False), (401, 0, False), (-1, 0, False), (1, -1, False),
    (400, code.CODE_AREA_BYTES - 400, True), (400, code.CODE_AREA_BYTES - 399, False),
])  # fmt: skip
def test_code_read_bounds(length, offset, ok):
    pkt = bytes([p.COMM_LISP_READ_CODE]) + struct.pack(">ii", length, offset)
    if ok:
        check_packet(pkt)
    else:
        with pytest.raises(SafetyRefusal):
            check_packet(pkt)


def stream_pkt(offset=0, total=10, mode=1, data=b"(print 1)\n"):
    return bytes([p.COMM_LISP_STREAM_CODE]) + struct.pack(">iib", offset, total, mode) + data


def test_stream_checks():
    with pytest.raises(SafetyRefusal):
        check_packet(stream_pkt())
    check_packet(stream_pkt(), code_stream=True)
    for pkt in (stream_pkt(mode=0), stream_pkt(mode=2), stream_pkt(offset=5), stream_pkt(offset=-1),
                stream_pkt(data=b"")):  # fmt: skip
        with pytest.raises(SafetyRefusal):
            check_packet(pkt, code_stream=True)


def test_the_upload_flag_doesnt_open_other_commands():
    with pytest.raises(SafetyRefusal):
        check_packet(stream_pkt(), code_write=True)
    with pytest.raises(SafetyRefusal):
        check_packet(write_pkt(0), code_stream=True)


# ---------------------------------------------------------------------- reading and backing up
def test_read_code_returns_the_stored_script(board):
    fake, prog = board()
    assert code.read_code(prog) == code.pack(STORED_SCRIPT)


def test_read_code_on_an_empty_board(board):
    fake, prog = board(script_present=False)
    assert code.read_code(prog) == b""
    path = code.save_backup(prog, b"", "manual")
    doc = code.load_backup(path)
    assert doc["empty"] and doc["content"] == b""


def test_a_multi_chunk_read(board):
    fake, prog = board()
    content = code.pack(big_script(3000))
    img = code.image(content)
    fake.code_area[:len(img)] = img
    assert code.read_code(prog) == content


def test_backup_round_trip_and_damage_is_caught(board, tmp_path):
    fake, prog = board()
    path = code.save_backup(prog, code.read_code(prog), "manual")
    doc = code.load_backup(path)
    assert doc["uuid"] == fake.uuid.hex() and doc["kind"] == "lisp-code" and doc["content"] == code.pack(STORED_SCRIPT)
    assert code.list_backups(prog.backup_dir, fake.uuid.hex()) == [path]
    raw = json.loads(path.read_text())
    raw["b64"] = raw["b64"][:-4] + "AAAA"
    path.write_text(json.dumps(raw))
    with pytest.raises(SafetyRefusal, match="checksum"):
        code.load_backup(path)


# ---------------------------------------------------------------------- storing
def test_store_writes_reads_back_and_leaves_the_script_stopped(board):
    fake, prog = board()
    content = code.pack(big_script(1000))
    res = code.store(prog, content)
    assert fake.stored_content() == content and res["bytes"] == len(content)
    img_len = len(code.image(content))
    assert fake.code_erases == 1
    assert fake.code_writes == list(range(0, img_len, code.WRITE_CHUNK))  # each chunk once, in order
    assert not fake.script_running and prog.script_paused
    assert code.load_backup(res["code_backup"])["content"] == code.pack(STORED_SCRIPT)
    assert prog.start() and fake.script_running
    fake.power_cycle()
    assert fake.script_running and fake.stored_content() == content


def test_store_refused_while_the_motor_turns(board):
    fake, prog = board()
    fake.erpm = 3000
    with pytest.raises(SafetyRefusal, match="not idle"):
        code.store(prog, code.pack(SCRIPT))
    assert fake.code_erases == 0 and fake.code_writes == []


def test_a_failed_backup_stops_before_the_erase(board, monkeypatch):
    fake, prog = board()

    def broken(*_):
        raise SafetyRefusal("disk full")

    monkeypatch.setattr(code, "save_backup", broken)
    with pytest.raises(SafetyRefusal):
        code.store(prog, code.pack(SCRIPT))
    assert fake.code_erases == 0 and fake.code_writes == []


def test_a_failed_erase_writes_nothing(board):
    fake, prog = board(code_erase_fails=True)
    with pytest.raises(VerifyFailed, match="restore"):
        code.store(prog, code.pack(SCRIPT))
    assert fake.code_erases == 1 and fake.code_writes == [] and prog.script_paused


def test_a_failed_write_is_not_retried_and_the_board_boots_without_a_script(board):
    fake, prog = board(code_write_fails_at=code.WRITE_CHUNK)
    with pytest.raises(VerifyFailed, match="sky code restore"):
        code.store(prog, code.pack(big_script(1000)))
    assert fake.code_writes == [0, code.WRITE_CHUNK]
    fake.power_cycle()
    assert not fake.script_running and fake.stored_content() is None


def test_a_silent_write_is_not_retried(board):
    fake, prog = board(code_write_silent_at=0)
    with pytest.raises(VerifyFailed, match="Not retried"):
        code.store(prog, code.pack(SCRIPT))
    assert fake.code_writes == [0]


def test_a_wrong_read_back_is_caught(board, monkeypatch):
    fake, prog = board()
    original, calls = code.read_code, []

    def read_then_corrupt(prog_):
        calls.append(1)
        fake.code_read_corrupt = len(calls) > 1  # the backup reads cleanly, the read-back doesn't
        return original(prog_)

    monkeypatch.setattr(code, "read_code", read_then_corrupt)
    with pytest.raises(VerifyFailed, match="doesn't read back"):
        code.store(prog, code.pack(SCRIPT))


def test_restore_puts_the_old_script_back(board):
    fake, prog = board()
    res = code.store(prog, code.pack(SCRIPT))
    code.store(prog, code.load_backup(res["code_backup"])["content"])
    assert fake.stored_content() == code.pack(STORED_SCRIPT)


def test_store_on_an_empty_board(board):
    fake, prog = board(script_present=False)
    res = code.store(prog, code.pack(SCRIPT))
    assert code.load_backup(res["code_backup"])["empty"] and fake.stored_content() == code.pack(SCRIPT)


def test_the_largest_script_fits_exactly(board):
    fake, prog = board()
    content = code.pack("x" * (code.MAX_CONTENT_BYTES - 3))
    code.store(prog, content)
    assert fake.stored_content() == content


# ---------------------------------------------------------------------- trying from RAM
def test_try_streams_each_chunk_once(board):
    fake, prog = board()
    text = big_script(1000)
    assert code.try_script(prog, text) == len(text.encode())
    assert fake.stream_starts == 1 and bytes(fake.streamed) == text.encode()
    assert fake.code_erases == 0 and fake.code_writes == []
    assert fake.stored_content() == code.pack(STORED_SCRIPT)  # nothing stored


def test_try_refused_while_the_motor_turns(board):
    fake, prog = board()
    fake.erpm = 3000
    with pytest.raises(SafetyRefusal):
        code.try_script(prog, SCRIPT)
    assert fake.stream_starts == 0


def test_a_refused_stream_chunk_is_not_retried(board):
    fake, prog = board(stream_fails_at=0)
    with pytest.raises(VerifyFailed, match="Power-cycle"):
        code.try_script(prog, SCRIPT)
    assert fake.stream_starts == 1


def test_try_refuses_an_empty_script(board):
    fake, prog = board()
    with pytest.raises(SafetyRefusal):
        code.try_script(prog, "")


# ---------------------------------------------------------------------- the `sky code` commands
@pytest.fixture
def sky(board, tmp_path, capsys, monkeypatch):
    from sky_programmer import cli

    fake, prog = board()
    monkeypatch.setattr("sys.stdin", open("/dev/null"))

    def run(*argv):
        rc = cli.main(["--port", "fake", "--backup-dir", str(prog.backup_dir), *argv],
                      serial_factory=lambda _port: fake)  # fmt: skip
        out = capsys.readouterr()
        return rc, out.out + out.err

    return fake, run


def test_cli_store_and_restore(sky, tmp_path):
    fake, run = sky
    script = tmp_path / "new.lisp"
    script.write_text(SCRIPT)
    rc, out = run("code", "store", str(script), "--engine-stopped", "--start")
    assert rc == 0, out
    assert "all" in out and "Script started" in out and fake.script_running
    assert fake.stored_content() == code.pack(SCRIPT)
    old = out.split("Previous script backed up: ")[1].split("\n")[0]
    rc, out = run("code", "restore", old, "--engine-stopped")
    assert rc == 0, out
    assert fake.stored_content() == code.pack(STORED_SCRIPT) and "not running" in out


def test_cli_store_needs_the_statement(sky, tmp_path):
    fake, run = sky
    script = tmp_path / "new.lisp"
    script.write_text(SCRIPT)
    rc, out = run("code", "store", str(script))
    assert rc == 2 and "--engine-stopped" in out and fake.code_erases == 0


def test_cli_refuses_a_forbidden_script(sky, tmp_path):
    fake, run = sky
    script = tmp_path / "bad.lisp"
    script.write_text("(image-save)")
    rc, out = run("code", "store", str(script), "--engine-stopped")
    assert rc == 2 and "image-save" in out and fake.code_erases == 0


def test_cli_try_and_backup(sky, tmp_path):
    fake, run = sky
    script = tmp_path / "t.lisp"
    script.write_text(SCRIPT)
    rc, out = run("code", "try", str(script), "--engine-stopped")
    assert rc == 0 and "Nothing was stored" in out and fake.stream_starts == 1
    rc, out = run("code", "backup")
    assert rc == 0 and "Script backup saved" in out
    rc, out = run("code", "backups")
    assert rc == 0 and "manual" in out


def test_cli_restore_refuses_another_boards_backup(sky, tmp_path):
    fake, run = sky
    rc, out = run("code", "backup")
    path = out.split(": ")[-1].strip()
    doc = json.loads(open(path).read())
    doc["uuid"] = "00" * 12
    other = tmp_path / "other.json"
    other.write_text(json.dumps(doc))
    rc, out = run("code", "restore", str(other), "--engine-stopped")
    assert rc == 2 and "UUID" in out and fake.code_erases == 0


def test_cli_restore_refuses_an_empty_backup(board, tmp_path, capsys, monkeypatch):
    from sky_programmer import cli

    fake, prog = board(script_present=False)
    path = code.save_backup(prog, b"", "manual")
    monkeypatch.setattr("sys.stdin", open("/dev/null"))
    rc = cli.main(["--port", "fake", "--backup-dir", str(prog.backup_dir), "code", "restore", str(path),
                   "--engine-stopped"], serial_factory=lambda _port: fake)  # fmt: skip
    assert rc == 2 and "nothing to restore" in capsys.readouterr().err and fake.code_erases == 0


def test_cli_restore_with_imports_needs_a_second_yes(sky, tmp_path):
    fake, run = sky
    rc, out = run("code", "backup")
    path = out.split(": ")[-1].strip()
    doc = json.loads(open(path).read())
    content = SCRIPT.encode() + b"\0" + struct.pack(">h", 1) + b"lib\0" + struct.pack(">ii", 20, 4) + b"\0" * 4
    import base64
    import hashlib

    doc["b64"], doc["sha256"] = base64.b64encode(content).decode(), hashlib.sha256(content).hexdigest()
    native = tmp_path / "native.json"
    native.write_text(json.dumps(doc))
    rc, out = run("code", "restore", str(native), "--engine-stopped")
    assert rc == 2 and "reboot over and over" in out and fake.code_erases == 0
    rc, out = run("code", "restore", str(native), "--engine-stopped", "--native-ok")
    assert rc == 0, out
    assert fake.stored_content() == content


def test_comms_errors_are_reported(board):
    fake, prog = board()
    fake.dead = True
    with pytest.raises(CommsError):
        code.read_code(prog)
