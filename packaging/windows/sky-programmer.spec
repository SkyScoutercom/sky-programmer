# PyInstaller build of the Windows app: `pyinstaller --noconfirm packaging/windows/sky-programmer.spec`
#
# One folder holding two programs: "SKY programmer.exe" (the window) and sky.exe (the terminal tool, which is where the
# overrides the window doesn't offer live). A folder rather than a single .exe, because a single .exe unpacks all of Qt
# to a temporary folder at every start, which is slow and is what antivirus tools tend to flag.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

here = Path(SPECPATH)
icon = str(here / "sky-programmer.ico")
datas = collect_data_files("sky_programmer")  # layouts and images. The self-test proves they arrived.
hidden = ["sky_programmer.layouts", "serial.tools.list_ports"]

gui = Analysis([str(here / "gui_entry.py")], datas=datas, hiddenimports=hidden, excludes=["tkinter"])
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="SKY programmer", icon=icon, console=False)

cli = Analysis([str(here / "cli_entry.py")], datas=datas, hiddenimports=hidden,
               excludes=["tkinter", "PySide6", "shiboken6", "sky_programmer.gui"])
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="sky", icon=icon, console=True)

# The GPL text is copied next to the programs after this build (see .github/workflows/windows.yml): data listed here
# would land in the _internal folder, where nobody looks for a license.
COLLECT(gui_exe, gui.binaries, gui.datas, cli_exe, cli.binaries, cli.datas, name="SKY programmer")
