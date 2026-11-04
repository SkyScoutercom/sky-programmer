<p align="center"><img src="docs/logo.png" alt="SkyScouter logo" width="240"></p>

# SKY programmer

SkyScouter's terminal tool for reading and configuring VESC motor controllers.

**Status: stage 1, not yet tested on a real board.** It is tested against a simulated VESC. Follow
[docs/BENCH_TEST.md](docs/BENCH_TEST.md) on the bench before relying on it.

It supports VESC firmware **7.00**. Other firmware can still use `status`, but config access is refused.

## Install

```bash
pipx install .        # or: pip install -e ".[dev]" in a virtual environment
sky --version
```

Close VESC Tool first, since only one program can use the port. Set the port with `--port` or `SKY_PORT`
if more than one board is plugged in.

## Commands

| Command | What it does |
|---|---|
| `sky status` | Board, firmware, live values, fault, script usage, BLDC/FOC mode |
| `sky monitor [--log run.csv] [--seconds N]` | Live values and messages printed by scripts or C code |
| `sky get [pattern] [--app] [--json]` | Read config fields, e.g. `sky get 'foc_*'` |
| `sky set field=value ...` | Change motor config, e.g. `sky set motor_type=FOC` |
| `sky backup` / `sky backups` | Save the motor and app config / list saved files |
| `sky diff <file>` | Compare the board with a backup |
| `sky restore <file>` | Write a backup's motor config back |
| `sky stop` / `sky start` | Pause the script and release the motor / run the stored script |
| `sky term <command>` | Read-only firmware terminal commands, e.g. `sky term faults` |
| `sky repl [-e expr]` | LispBM console; words that write flash, reach other boards or move the motor are blocked |

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

1. **Done, pending bench test:** status, monitor, get/set, backup/restore, REPL.
2. **C workflow:** build and upload C native libraries, running from RAM first.
3. **Independent of VESC Tool:** own uploader and motor detection.
4. **Firmware flashing:** last, and only with the checks listed in SAFETY.md.

App config writes are not included yet. App config is read and backed up, but only VESC Tool can write it.

## Development

```bash
pip install -e ".[dev]"
ruff check . && pytest
```

`tests/fake_vesc.py` is a simulated board with failures on demand, and any forbidden command it receives
fails the test. `tools/gen_layout.py` generates a firmware's config layout from its sources.
