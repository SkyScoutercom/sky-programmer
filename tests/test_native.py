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


# What `readelf -S -W` prints for the real hall_capture build (trimmed).
READELF_GOOD = """
  [Nr] Name              Type            Addr     Off    Size   ES Flg Lk Inf Al
  [ 0]                   NULL            00000000 000000 000000 00      0   0  0
  [ 1] .program_ptr      PROGBITS        00000000 001000 000004 00  WA  0   0  4
  [ 2] .init_fun         PROGBITS        00000004 001004 0001d0 00  AX  0   0  4
  [ 3] .data             PROGBITS        000001d4 0019e0 000000 00  WA  0   0  4
  [ 4] .bss              NOBITS          000001d4 000000 000000 00  WA  0   0  4
  [ 5] .got              PROGBITS        000001d4 0019e0 000000 00  WA  0   0  4
  [ 6] .text             PROGBITS        000001e0 0011e0 0001e5 01 AMS  0   0 16
  [ 7] .text.get_data    PROGBITS        000003c8 0013c8 00001c 00  AX  0   0  4
  [21] .comment          PROGBITS        00000000 001a5d 000045 01  MS  0   0  1
"""
GOOD_SYMBOLS = {"init": 4, "_text": 0}


def _with(name, addr, size, flags):
    return [*native._sections(READELF_GOOD), native.Section(name, addr, size, flags)]


def test_the_real_layout_passes():
    sections = native._sections(READELF_GOOD)
    assert [s.name for s in sections][:3] == [".program_ptr", ".init_fun", ".data"]
    assert native.elf_problems(sections, GOOD_SYMBOLS) == []
    nm = "00000004 T init\n00000010 t helper\n         U memcpy\n00000000 D _text\n"
    assert native._globals(nm) == GOOD_SYMBOLS


@pytest.mark.parametrize("section", [
    (".data.counter", 0x1E4, 4, "WA"),  # -fdata-sections: a static variable
    (".bss.calls.0", 0x1E8, 4, "WA"),
    (".data.rel.ro.local.fns", 0x1EC, 8, "WA"),  # a function-pointer table: addresses counted from 0
    (".got", 0x1F0, 8, "WA"),
    (".init_array", 0x1F8, 4, "WA"),
])  # fmt: skip
def test_writable_data_is_refused_whatever_its_name(section):
    problems = native.elf_problems(_with(*section), GOOD_SYMBOLS)
    assert any("writable data" in p for p in problems)


def test_init_must_be_where_the_loader_jumps():
    sections = [s for s in native._sections(READELF_GOOD) if s.name != ".init_fun"]
    assert any("INIT_FUN" in p for p in native.elf_problems(sections, {"init": 0x24, "_text": 0}))
    assert any("program_ptr" in p for p in native.elf_problems(native._sections(READELF_GOOD)[1:], GOOD_SYMBOLS))
    extra = {**GOOD_SYMBOLS, "helper": 9}
    assert any("helper" in p for p in native.elf_problems(native._sections(READELF_GOOD), extra))


def test_load_address_must_be_the_run_address():
    good = "  LOAD           0x001000 0x00000000 0x00000000 0x009e0 0x009e0 RWE 0x1000\n"
    assert native._load_matches_run(good)
    assert not native._load_matches_run(good.replace("0x00000000 0x00000000", "0x00000000 0x08000000"))


@pytest.mark.parametrize("source", [
    "void f(void) { FLASH->KEYR = 0x45670123; }", "void f(void) { FLASH_EraseSector(11, 2); }",
    "void f(void) { NVIC_SystemReset(); }", "void f(void) { SCB->AIRCR = 0x05FA0004; }",
    "void f(void) { VESC_IF->store_eeprom_var(0, 1); }", "void f(void) { VESC_IF->set_cfg(0); }",
])  # fmt: skip
def test_flash_reset_and_unset_calls_are_refused(tmp_path, source):
    with pytest.raises(SafetyRefusal, match="can't be stored"):
        native.check_sources(native.find_project(_project(tmp_path, source)))


def test_pin_mode_calls_warn(tmp_path):
    project = native.find_project(_project(tmp_path, "void f(void) { VESC_IF->set_pad_mode(0); }"))
    warnings = native.check_sources(project)
    assert len(warnings) == 1 and "gate-driver" in warnings[0]


def test_sources_outside_the_project_are_refused(tmp_path):
    makefile = ("TARGET = lib\nSOURCES = code.c\nSOURCES += ../shared/helper.c\n"
                "VESC_C_LIB_PATH ?= ../.c_libs/lispBM/c_libs/\ninclude $(VESC_C_LIB_PATH)rules.mk\n")  # fmt: skip
    with pytest.raises(SafetyRefusal, match="outside the project"):
        native.find_project(_project(tmp_path, makefile=makefile))


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
        assert result.matches_committed is True
        assert [w.split(":")[0] for w in result.warnings] == ["code.c uses `set_pad_mode`"]  # the hall input pin


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


# ---------------------------------------------------------------------- real builds of hall_capture variants
def _hall_variant(tmp_path, edit):
    """A copy of hall_capture with `edit(code)` applied, building against the real interface files."""
    real = native.find_project(HALL)
    folder = tmp_path / "hall_capture"
    folder.mkdir()
    for name in ("code.c", "hall_logic.h"):
        (folder / name).write_bytes((real.folder / name).read_bytes())
    (folder / "Makefile").write_text(f"TARGET = hall_capture\nSOURCES = code.c\nUSE_STLIB = yes\n"
                                     f"VESC_C_LIB_PATH ?= {real.c_libs}/\ninclude $(VESC_C_LIB_PATH)rules.mk\n")
    (folder / "code.c").write_text(edit((folder / "code.c").read_text()))
    return native.find_project(folder / "hall_capture.bin")


real_build = pytest.mark.skipif(not HALL.is_file(), reason="only on the bench laptop, with the vesc-starter project")


@real_build
def test_a_real_static_variable_is_refused(tmp_path):
    toolchain, version = _toolchain_or_skip()

    def edit(src):
        i = src.index("INIT_FUN(")
        j = src.index("{", i) + 1
        check = "\n\tif (++counter == 7) { return false; }\n"
        return src[:i] + "static int counter = 5;\n" + src[i:j] + check + src[j:]

    with pytest.raises(SafetyRefusal, match=r"writable data in \.data\.counter"):
        native.build(_hall_variant(tmp_path, edit), toolchain, version)


@real_build
def test_a_real_library_without_init_fun_is_refused(tmp_path):
    toolchain, version = _toolchain_or_skip()
    edit = lambda src: src.replace("INIT_FUN(lib_info *info) {", "bool init(lib_info *info) {")  # noqa: E731
    with pytest.raises(SafetyRefusal, match="address 4"):
        native.build(_hall_variant(tmp_path, edit), toolchain, version)


def test_edited_interface_files_are_refused(tmp_path, monkeypatch):
    project = native.find_project(_project(tmp_path))
    monkeypatch.setattr(native, "interface_changes", lambda c_libs: ["vesc_c_if.h"])
    with pytest.raises(SafetyRefusal, match="local changes"):
        native.build(project, tmp_path, native.TOOLCHAIN_VERSION)


def test_interface_changes_are_found_with_git(tmp_path):
    import subprocess

    c_libs = tmp_path / "c_libs"
    c_libs.mkdir()
    (c_libs / "vesc_c_if.h").write_text("void (*read_nvm)(void);\n")
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(c_libs), *cmd], check=True)
    (c_libs / "lib.so").write_bytes(b"\0")  # an untracked build output doesn't count
    assert native.interface_changes(c_libs) == []
    (c_libs / "vesc_c_if.h").write_text("void (*extra)(void);\nvoid (*read_nvm)(void);\n")
    assert native.interface_changes(c_libs) == ["vesc_c_if.h"]


def test_store_build_refuses_a_prebuilt_import(tmp_path, monkeypatch, capsys):
    full = "20cbb362687291242ab90b99f25fbfe8835540fc"
    monkeypatch.setattr(native, "build_imports", lambda text, folder: [("a.bin", "liba", None)])
    from sky_programmer import cli
    from sky_programmer.simulator import FakeVesc

    fake = FakeVesc()
    fake.prints_on_term = [f"Git Hash  : {full}"]
    (tmp_path / "a.bin").write_bytes(b"\x01\x02\x03\x04")
    script = tmp_path / "s.lisp"
    script.write_text('(import "a.bin" \'liba)\n(load-native-lib liba)\n')
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = cli.main(["--port", "fake", "--backup-dir", str(tmp_path / "bk"), "code", "store", str(script), "--build",
                   "--engine-stopped", "--native-ok"], serial_factory=lambda _p: fake)  # fmt: skip
    assert rc == 2 and "no source to build from" in capsys.readouterr().err and fake.code_erases == 0
