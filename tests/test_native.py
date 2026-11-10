"""Building native (C) libraries from source: project discovery, source rules, ELF checks, the interface commit."""
import io
from pathlib import Path

import pytest

from sky_programmer import code, native
from sky_programmer.errors import SafetyRefusal

HALL = Path.home() / "Desktop/Drone_ECU/vesc-starter/native/hall_capture/hall_capture.bin"


def _project(tmp_path, source="int x;", target="lib", makefile=None):
    c_libs = tmp_path / ".c_libs" / "lispBM" / "c_libs"
    c_libs.mkdir(parents=True, exist_ok=True)
    (c_libs / "vesc_c_if.h").write_text("")
    (c_libs / "rules.mk").write_text("")
    folder = tmp_path / target
    folder.mkdir()
    (folder / "code.c").write_text(source)
    (folder / "Makefile").write_text(makefile or f"TARGET = {target}\nSOURCES = code.c\n"
                                     "VESC_C_LIB_PATH ?= ../.c_libs/lispBM/c_libs/\n"
                                     "include $(VESC_C_LIB_PATH)rules.mk\n")
    (folder / f"{target}.bin").write_bytes(b"\x01\x02\x03\x04")
    return folder / f"{target}.bin"


def test_a_project_is_found_from_its_bin(tmp_path):
    project = native.find_project(_project(tmp_path))
    assert project.target == "lib" and project.c_libs == (tmp_path / ".c_libs/lispBM/c_libs").resolve()


def test_a_prebuilt_bin_has_no_project(tmp_path):
    (tmp_path / "x.bin").write_bytes(b"\0")
    assert native.find_project(tmp_path / "x.bin") is None
    other = _project(tmp_path, target="other")
    assert native.find_project(other.with_name("nope.bin")) is None  # the Makefile builds a different target


def test_missing_build_files_are_refused(tmp_path):
    bin_path = _project(tmp_path)
    (tmp_path / ".c_libs/lispBM/c_libs/vesc_c_if.h").unlink()
    with pytest.raises(SafetyRefusal, match="fetch_c_libs"):
        native.find_project(bin_path)


@pytest.mark.parametrize("call", sorted(native.REFUSED_CALLS))
def test_calls_that_write_flash_or_send_raw_commands_are_refused(tmp_path, call):
    project = native.find_project(_project(tmp_path, f"void f(void) {{ VESC_IF->{call}(0); }}"))
    with pytest.raises(SafetyRefusal, match=call):
        native.check_sources(project)


def test_motor_and_config_calls_warn(tmp_path):
    project = native.find_project(_project(tmp_path, "void f(void) { VESC_IF->set_cfg_float(1, 2.0f); }"))
    warnings = native.check_sources(project)
    assert len(warnings) == 1 and "set_cfg_float" in warnings[0]


def test_a_clean_source_passes(tmp_path):
    project = native.find_project(_project(tmp_path, 'void f(void) { VESC_IF->printf("x"); }'))
    assert native.check_sources(project) == []


OBJDUMP_GOOD = """
Sections:
Idx Name          Size      VMA       LMA       File off  Algn
  0 .program_ptr  00000004  00000000  00000000  00010000  2**2
  1 .text         000009d0  00000004  00000004  00010004  2**4
"""


def test_elf_checks():
    sections = native._sections(OBJDUMP_GOOD)
    assert sections == {".program_ptr": 4, ".text": 0x9D0}
    assert native.elf_problems(sections, {"init", "_text"}) == []
    assert any("GOT" in p for p in native.elf_problems({**sections, ".got": 8}, {"init"}))
    assert any(".bss" in p for p in native.elf_problems({**sections, ".bss": 4}, {"init"}))
    assert any("program_ptr" in p for p in native.elf_problems({".text": 4}, {"init"}))
    assert any("helper" in p for p in native.elf_problems(sections, {"init", "helper"}))
    nm = "00000005 T init\n00000010 t helper\n         U memcpy\n00000000 T _text\n"
    assert native._globals(nm) == {"init", "_text"}


def test_the_interface_files_must_match_the_board():
    full = "20cbb362687291242ab90b99f25fbfe8835540fc"
    native.require_matching_interface(full, full)
    native.require_matching_interface(full, full[:7])
    with pytest.raises(SafetyRefusal, match="e877eae"):
        native.require_matching_interface("e877eae" + "0" * 33, full)
    with pytest.raises(SafetyRefusal, match="can't tell"):
        native.require_matching_interface(None, full)


def test_the_board_commit_is_read_from_fw_info():
    class Prog:
        def terminal(self, command, wait=0.5):
            assert command == "fw_info"
            return ["Git Branch: release_7_00", "Git Hash  : 20cbb362687291242ab90b99f25fbfe8835540fc", "Compiler: 7.3"]

    assert native.board_commit(Prog()) == "20cbb362687291242ab90b99f25fbfe8835540fc"


def test_a_missing_toolchain_says_where_to_put_it(monkeypatch, tmp_path):
    monkeypatch.setattr(native, "toolchain_candidates", lambda: [tmp_path / "none"])
    with pytest.raises(SafetyRefusal, match="13.3.rel1"):
        native.find_toolchain()


def test_a_fresh_build_replaces_the_file_on_disk(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"old")
    text = '(import "a.bin" \'liba)\n(load-native-lib liba)\n'
    content, _ = code.pack_script(text, tmp_path, built={"a.bin": b"new!"})
    assert code.unpack_imports(content) == [("liba", b"new!")]


def _toolchain_or_skip():
    try:
        return native.find_toolchain()
    except SafetyRefusal:
        pytest.skip("the Arm toolchain isn't installed")


@pytest.mark.skipif(not HALL.is_file(), reason="only on the bench laptop, with the vesc-starter project")
def test_building_the_real_hall_capture_library():
    toolchain, version = _toolchain_or_skip()
    project = native.find_project(HALL)
    before = sorted(p.name for p in project.folder.iterdir()) + sorted(p.name for p in project.c_libs.rglob("*.so"))
    result = native.build(project, toolchain, version)
    after = sorted(p.name for p in project.folder.iterdir()) + sorted(p.name for p in project.c_libs.rglob("*.so"))
    assert after == before  # the project folder and the build files are left alone
    assert result.name == "hall_capture" and len(result.data) == HALL.stat().st_size  # the first word is the program
    # pointer slot, zero until the loader fills it
    assert result.commit and result.commit.startswith("20cbb362")
    if version == native.TOOLCHAIN_VERSION:
        assert result.matches_committed is True and result.warnings == []


def _store_build(tmp_path, monkeypatch, board_hash, fresh_commit):
    from sky_programmer import cli
    from sky_programmer.simulator import FakeVesc

    fake = FakeVesc()
    fake.prints_on_term = [f"Git Hash  : {board_hash}"]
    (tmp_path / "a.bin").write_bytes(b"\x01\x02\x03\x04")
    script = tmp_path / "s.lisp"
    script.write_text('(import "a.bin" \'liba)\n(load-native-lib liba)\n')
    fresh = native.Build("a", b"\x09" * 8, "13.3.1", fresh_commit, False)
    monkeypatch.setattr(native, "build_imports", lambda text, folder: [("a.bin", "liba", fresh)])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = cli.main(["--port", "fake", "--backup-dir", str(tmp_path / "bk"), "code", "store", str(script), "--build",
                   "--engine-stopped", "--native-ok"], serial_factory=lambda _p: fake)  # fmt: skip
    return rc, fake


def test_cli_store_build_refuses_a_mismatched_interface(tmp_path, monkeypatch, capsys):
    rc, fake = _store_build(tmp_path, monkeypatch, "e877eae" + "0" * 33, "20cbb362687291242ab90b99f25fbfe8835540fc")
    out = capsys.readouterr()
    assert rc == 2 and "e877eae" in out.err and "differs from this build" in out.out and fake.code_erases == 0


def test_cli_store_build_stores_the_fresh_build(tmp_path, monkeypatch, capsys):
    full = "20cbb362687291242ab90b99f25fbfe8835540fc"
    rc, fake = _store_build(tmp_path, monkeypatch, full, full)
    assert rc == 0, capsys.readouterr()
    assert code.unpack_imports(fake.stored_content()) == [("liba", b"\x09" * 8)]
