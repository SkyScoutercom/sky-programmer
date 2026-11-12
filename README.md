<p align="center"><img src="docs/logo.png" alt="SkyScouter logo" width="240"></p>

# SKY programmer

SkyScouter's terminal and desktop tool for reading and configuring VESC motor controllers.

**Status: stage 1, bench-tested on one board.** Reading, writing, restoring and the safety checks passed on a
Flipsky_110_100 on Ubuntu: see [docs/BENCH_RESULTS.md](docs/BENCH_RESULTS.md). BLDC to FOC and Windows haven't been run
on hardware yet. On another board, follow [docs/BENCH_TEST.md](docs/BENCH_TEST.md) on the bench before relying on it.

It supports VESC firmware **7.00**. Other firmware can still use `status`, but config access is refused.

## Install

```bash
pipx install .        # or: pip install -e ".[dev]" in a virtual environment
sky --version
```

Close VESC Tool first, since only one program can use the port. Set the port with `--port` or `SKY_PORT`
if more than one board is plugged in.

## Desktop app

It needs Python 3.12 or newer, because PySide6 crashes on older versions. The `sky` command works from Python 3.10.

```bash
pipx install --force --editable '.[gui]'   # installs `sky` and `sky-gui`, running from this folder
sky-gui --simulate                         # try it first on a simulated board (also in the app: "Try with a simulated board")
sky-gui --install-launcher                 # add SKY programmer to the Ubuntu app grid
sky-gui                                    # the real thing
```

| Page | What it does |
|---|---|
| Dashboard | Live values and charts, CSV recording, pause and start the script |
| Configuration | Browse and search motor config, edit, review and write. App config is read only |
| Backups | Back up, compare with the board, restore |
| Scripts | Back up the stored Lisp script, try a file from RAM, store a file (optionally building its native libraries from source), restore a script backup: the same checks as `sky code`. **Rescue a boot-looping board** is on the start screen, with no board connected |
| Console | Board messages, read-only terminal commands, the filtered REPL |
| Updates | Status, pull and reset for this source folder, as on the SKY kiosk. Works with no board connected |

The ☀/☾ button in the header switches between day and night, and the app remembers your choice.

**Updates.** Pull only moves forward, so if this folder and GitHub have both changed, nothing is merged. Reset makes the
folder match GitHub after you've seen what would be lost and typed `RESET`. Both need the board disconnected, and if the
app runs from that folder you must restart it before connecting again. It uses your normal git login: if a pull can't
log in, run `gh auth setup-git`.

It runs the same engine and the same checks as `sky`, and a write always ends in a typed `YES`. It offers none of the
overrides (`--allow-risky`, `--accept-drift`, `--allow-default-foc-params`, `--allow-other-board`, `--allow-motor`,
`--force`), so those stay deliberate terminal commands. Fields that always need one are locked in the table, with the
reason beside them. See [SAFETY.md](SAFETY.md#the-desktop-app).

### Ubuntu: stop ModemManager taking the board

After a VESC is plugged in, Ubuntu's ModemManager opens its port for a few seconds to test it as a modem, and the app
loses replies while it does. Stop it for VESC boards once:

```bash
sudo cp packaging/linux/99-sky-programmer.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

Then unplug the board and plug it back in. The app also refuses to connect while another program of yours, such as a
`sky` command or a script, has the port open, and names it.

### Windows

1. Open the [Releases page](https://github.com/SkyScoutercom/sky-programmer/releases). No GitHub account is needed.
2. Under the newest release, open **Assets** and download `SKY-programmer-setup-<version>.exe`.
3. Run it. It needs no administrator rights, and it adds SKY programmer and a terminal shortcut to the Start menu.

The installer isn't code-signed yet, so Windows SmartScreen warns that the publisher is unknown: choose **More info**,
then **Run anyway**. Every push to `stage-1` also builds an installer and a portable zip under Actions, but downloading
those needs a GitHub login.

The port is a USB COM port, such as `COM7`. Backups are in `%LOCALAPPDATA%\sky-programmer\backups`. A packaged build has
no Updates page: a new version is a new download. `sky.exe` is the terminal tool, in the same folder.

To check an install, run `"SKY programmer.exe" --self-test report.txt`. It uses a simulated board and touches no hardware.

## Commands

The board can be on USB (`/dev/ttyACM*`, a COM port on Windows) or on its **UART** pins through a 3.3 V USB-to-UART
adapter (`/dev/ttyUSB*`), at 115200 baud: see section K of [docs/BENCH_TEST.md](docs/BENCH_TEST.md). With more than one
port present, pass `--port`.

| Command | What it does |
|---|---|
| `sky status` | Board, firmware, live values, fault, script usage, BLDC/FOC mode |
| `sky monitor [--log run.csv] [--seconds N]` | Live values and messages printed by scripts or C code |
| `sky get [pattern] [--app] [--json]` | Read config fields, e.g. `sky get 'foc_*'` |
| `sky set field=value ...` | Change motor config, e.g. `sky set motor_type=FOC` |
| `sky backup [--accept-drift]` / `sky backups` | Save the motor and app config / list saved files |
| `sky diff <file>` | Compare the board with a backup |
| `sky restore <file>` | Write a backup's motor config back |
| `sky stop` / `sky start` | Pause the script and release the motor / run the stored script |
| `sky term <command>` | Read-only firmware terminal commands, e.g. `sky term faults` |
| `sky repl [-e expr]` | LispBM console; words that write flash, reach other boards or move the motor are blocked |
| `sky code backup` / `sky code backups` | Save the board's stored Lisp script to a file / list saved scripts |
| `sky code try <file.lisp>` | Run a script from RAM without storing it; a power-cycle brings back the stored one |
| `sky code store <file.lisp> [--start]` | Store a script: backs up the old script and the config, erases, writes, reads every byte back |
| `sky code restore <backup> [--start]` | Store one of this board's script backups again |
| `sky code rescue --engine-stopped` | Wait for the board to power up, then erase its stored script inside the start-up guard window: breaks a native-library boot loop |
| `sky code build <file.lisp>` | Build the script's native libraries from their C source and check them: interface files pinned to the firmware, no calls that write flash, an ELF the loader can run, and whether the committed `.bin` matches. Runs the project's Makefile in a temporary copy; nothing goes to a board |
| `sky code store <file.lisp> --build` | Store with libraries freshly built from source, after checking their interface files match the board's firmware commit |

### Uploading a Lisp script

Try it first with `sky code try`, then store it with `sky code store`. Storing asks for the same engine-stopped `YES`
as a config write. Every erase and write releases the motor for up to 5 s, and the board runs the stored script at
every power-up. A store that fails part-way leaves the board with no script: it still boots, and the command prints the
`sky code restore` line that puts the old one back.

Scripts that `import` files, such as native libraries, can be stored but not tried, and storing them asks a second
YES. The files are packed exactly as VESC Tool packs them. A native library that crashes when it loads makes the board
reboot over and over, so `sky code store` puts a start-up guard at the top of such a script: at a power-up with the
motor still it waits 10 s before anything loads (after a reset with the engine running, it doesn't wait). A native
library is machine code with full access to the board, so store only libraries you built or trust: each one is listed
with its SHA-256. If a library ever boot-loops the board, run `sky code rescue --engine-stopped` and
power-cycle: it erases the script inside that window, and the board starts with none (see [SAFETY.md](SAFETY.md); not
yet tried with a crashing library). The design and the firmware facts behind it are in
[docs/STAGE2_DESIGN.md](docs/STAGE2_DESIGN.md).

### Changing from BLDC to FOC

```bash
sky backup                                   # first time: sets the baseline
sky set motor_type=FOC --dry-run             # see what would change
sky set motor_type=FOC                       # asks you to confirm, then writes and verifies
```

FOC needs the motor's measured resistance, inductance and flux linkage. If they are still the firmware
defaults, the app refuses, so give the measured values in the same command:

```bash
sky set motor_type=FOC foc_motor_r=0.0081 foc_motor_l=4.97e-6 foc_motor_flux_linkage=0.0018
```

Those example numbers were measured on a Mini board, so check them for yours.

A write leaves the script paused. Run `sky start` or power-cycle to run it again. A REPL expression also resumes it.

The first write needs a baseline, so run `sky backup` first, ideally just after power-up with the engine stopped.
The read-back confirms the board's working copy, so power-cycle and check with `sky get` to prove the save to flash.

Changes that remove protection or go above the board's rating need `--allow-risky`. The ratings are in
`BOARD_RATINGS` in `config.py`. Values VESC Tool would refuse are never accepted.

Exit codes: `0` done, `2` refused (nothing was written), `3` communication problem, `4` the write did
not verify.

## How the board is protected

See **[SAFETY.md](SAFETY.md)**. In short: firmware and bootloader commands can't be sent at all, and every
config write is checked, backed up, written once, read back and rolled back if it comes out wrong. Values known to
hang a board at boot are refused with no override.

## Roadmap

1. **Done, bench-tested on one board:** status, monitor, get/set, backup/restore, REPL, in the terminal and the desktop app.
2. **Script upload:** Lisp scripts done and bench-tested in the terminal, and on the desktop app's Scripts page (same engine,
   not yet run on hardware). Next: native libraries, which firmware 7.00 can't run from RAM first, then building C libraries.
3. **Independent of VESC Tool:** own uploader and motor detection.
4. **Firmware flashing:** last, and only with the checks listed in SAFETY.md.

App config writes are not included yet. App config is read and backed up, but only VESC Tool can write it.

## Development

```bash
pip install -e ".[dev]"
ruff check . && pytest
```

`src/sky_programmer/simulator.py` is a simulated board with failures on demand, and any forbidden command it receives
fails the test. Desktop tests run the real window with no display (`QT_QPA_PLATFORM=offscreen`). `tools/gen_layout.py` generates a firmware's config layout from its sources.

## License

Copyright (C) 2026 SkyScouter

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License
as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It
is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for details.

The config layouts in `src/sky_programmer/layouts` are generated from the [VESC firmware](https://github.com/vedderb/bldc),
which is also licensed under GPL-3.0.
