# How SKY programmer protects the board

File and line names like `flash_helper.c` refer to VESC firmware source, branch `release_7_00`.

## What this does and doesn't promise

**The app has no command that can erase or replace the firmware or bootloader.** The worst result of a bad
or interrupted config write should be a board that boots on default settings and is fixed with `sky restore`.

It can't promise everything:

- **Physical damage is still possible.** A config can be valid and still wrong for your motor. The firmware's
  hardware limits and these checks reduce that risk but don't remove it.
- **The engine is invisible to the board** once a relay disconnects the alternator, so `sky` asks you to state it
  is stopped. That statement is yours to get right.
- **The REPL filter prevents accidents, not determined misuse.** A symbol built at runtime can get past it.
- **VESC Tool can still do anything.** These protections only apply inside this app.
- **None of this has been run on real hardware yet.** See [docs/BENCH_TEST.md](docs/BENCH_TEST.md).

## What can brick a VESC, and what the app does about it

| Risk | Firmware behaviour | Protection |
|---|---|---|
| Erasing the bootloader | `COMM_ERASE_BOOTLOADER` erases that flash sector | Permanently blocked |
| Jumping to a missing bootloader | `flash_helper_jump_to_bootloader` jumps to whatever is at `0x080E0000` | Permanently blocked |
| Flashing wrong or bad firmware | `COMM_ERASE_NEW_APP`, `COMM_WRITE_NEW_APP_DATA` and their variants | Permanently blocked in this stage |
| Doing the above to another board | `COMM_FORWARD_CAN`, and the `_ALL_CAN` variants | Permanently blocked |
| Flashing other chips through the SWD bridge | `COMM_BM_*` | Permanently blocked |
| Terminal commands that spin the motor or reboot | `foc_openloop`, `measure_*`, `rebootwdt` and others | Only 12 read-only terminal commands are allowed |
| REPL words that store config or load code | `conf-store`, `store-backup`, `load-native-lib` and others | Refused before sending |
| Wrong layout: values land in the wrong fields | Layout differs per firmware | Exact firmware match only, plus signature and size checks; no fallback, no download at runtime |
| Power loss during a config write | Stored with a CRC. On a bad CRC the firmware loads defaults (`conf_general_read_mc_configuration`) | Backup always taken first; `sky restore` fixes it |
| Bad value, e.g. flux = -2 or odd pole count | Firmware only limits some fields | Rules refuse new invalid values |
| Writing while the motor turns | The firmware releases the motor during the write | Refused unless idle on two samples, checked again just before writing |
| Script changing things during a write | `conf-set` changes live config | Script paused before writing; config read twice and compared |
| Storing a script's temporary changes | A write stores the whole live config | Differences from the last backup are refused unless `--accept-drift` |
| Weak supply during a flash write | Brown-out can interrupt it | Bus voltage must be inside the configured range (USB-only power is allowed) |
| Repeating a write that may have worked | Wears flash and hides the real state | Never retried; the read-back decides |
| Pausing the script with the engine running | The script owns throttle and kill | `sky stop` refuses if the motor is turning |

Blocking uses an allow-list: `check_packet()` in `safety.py` sees every packet, and any command not reviewed
and listed is refused. Only 11 of the 157 commands are allowed.

## Every config write follows these steps

1. Read the config twice and require identical results.
2. Parse and range-check the values. Only the named fields' bytes change.
3. Check the result with the validity rules, and for BLDC to FOC, that the FOC values were measured.
4. Show the changes and ask for confirmation that the engine is stopped.
5. Pause the script, then check the motor is idle and the supply is in range.
6. Re-read and require that nothing changed since planning.
7. Save a backup and read that back. If it fails, nothing is written.
8. Write **once**.
9. Read back and compare every field:
   - All as planned: done, and saved as the new baseline.
   - Only requested fields differ: the firmware limited them, which is reported.
   - Unrelated fields changed: rolled back once, then reported.
   - Nothing changed: reported as failed.
   - No answer: reported as unknown, with the backup path. Not retried.

## If a board stops responding

Try these in order. Steps 2 and 3 come from the firmware source and have not been tried on Flipsky boards yet.

1. **Power-cycle it** with the engine stopped, then run `sky status`.
2. **Script or C library crash:** the firmware doesn't start Lisp after a watchdog reset
   (`lispif_init`), so USB should stay usable. Fix or erase the script in VESC Tool.
3. **Config and script wipe:** connect HALL2 to SWCLK while powering up. The firmware resets the config and erases
   the stored Lisp and QML (`should_reset_config` in `main.c`). Afterwards use `sky restore`. Check that your board
   exposes these pins first.
4. **SWD:** an ST-Link on the board's SWD pads can reflash the firmware and bootloader.

## Rules for later stages

These aren't built yet, and they are requirements for building them.

**C libraries (stage 2).** C code runs with full access to the chip, so no tool can make it harmless. Also, stored
Lisp always starts at boot.

- Run new code from RAM first (`COMM_LISP_STREAM_CODE`) and store it only after it stays healthy.
- Refuse to build code that calls flash functions directly.
- Check size against the 128 kB limit, and check that the upload reads back correctly.

**Firmware flashing (stage 4).**

- Require an exact hardware name match against the image, and a known image from the release.
- Check the bootloader is present before erasing anything.
- Require a stable supply, a stopped motor and a config backup.
- Offer an SWD-programmer recovery check before the first flash.
