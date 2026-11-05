# How SKY programmer protects the board

File and line names like `flash_helper.c` refer to VESC firmware source, branch `release_7_00`.

## What this does and doesn't promise

**The app sends no packet that erases or replaces the firmware or bootloader, and it refuses the config values known to
hang a board at boot.** A bad or interrupted config write should leave a board that boots on default settings and is
fixed with `sky restore`.

It can't promise everything:

- **Physical damage is still possible.** A config can be valid and still wrong for your motor. The firmware's own
  limits sit well above these boards' ratings (300 A and 150 V on the 110100), and `_no_limits` firmware builds have
  none, so the app's rating table is the main check, and `--allow-risky` goes past it.
- **The list of boot-hanging values comes from reading the firmware, not from trying them.** One independent review
  found one route (BLDC or DC switching frequency) and no other, but that doesn't prove there is no other.
- **The engine is invisible to the board** once a relay disconnects the alternator, so a write or start asks you to
  state it is stopped. That statement is yours to get right. `sky stop` doesn't ask, though the desktop app does.
- **The REPL can't see inside your own script's functions.** It checks the words you type, so a function your script
  defines runs whatever it contains.
- **A failed flash save goes unreported.** The firmware applies the new config in RAM and acknowledges even if the
  save failed, so only a power-cycle proves a change was stored.
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
| Boot hang from a bad switching frequency | `m_bldc_f_sw_*` and `m_dc_f_sw` set the PWM timer period (`SYSTEM_CORE_CLOCK / f_sw` in `mcpwm_init`). At 0 the timer stops and `do_dc_cal` spins forever, before USB and the watchdog start | Ranges enforced with no override, including on restore and on switching mode |
| REPL words that reach flash, other boards, or any packet | `cmds-proc` feeds raw packets to the command processor, including erase-bootloader; `image-save`, `eeprom-store-*`, `can-send-*`, `conf-store` and others | Every firmware word is refused unless reviewed as read-only; motor words need `--allow-motor`; `read`, `eval`, comments and backslashes are refused |
| REPL meta commands that freeze a script | `:pause`, `:env` and `:symbols` leave the evaluator paused, which takes away throttle and kill control | Only `:help`, `:info`, `:ctxs` and `:verb` are allowed |
| A "read" that restarts a paused script | `COMM_LISP_GET_STATS` always calls `lbm_continue_eval`; a REPL expression also resumes | Stats command removed from the app; the REPL warns |
| Values far above the board's rating | Firmware limits are 300 A and 150 V on the 110100 | Going above the app's rating table, or raising on an unknown board, needs `--allow-risky` |
| Removing protection | Gate-driver overcurrent settings, phase shorting (`foc_short_ls_on_zero_duty`, `l_max_erpm_fbrake*`), motor temperature limits | Needs `--allow-risky` |
| Values VESC Tool would refuse | VESC Tool's parameter file sets a range for each field | Enforced with no override; a field with no known range needs `--allow-risky` |
| A new voltage range that excludes the present supply | Later writes would be refused | Refused |
| Wrong layout: values land in the wrong fields | Layout differs per firmware | Exact firmware match only, plus signature and size checks; no fallback, no download at runtime |
| Power loss during a config write | Stored with a CRC. On a bad CRC the firmware loads defaults (`conf_general_read_mc_configuration`) | Backup always taken first; `sky restore` fixes it |
| Bad value, e.g. flux = -2 or odd pole count | Firmware only limits some fields | Rules refuse new invalid values |
| Writing while the motor turns | The firmware releases the motor during the write | Refused unless idle on two samples, checked before the script is paused and again before writing |
| Script changing things during a write | `conf-set` changes live config | Script paused before writing; a failed pause stops the write; config read twice and compared |
| Storing a script's temporary changes | A write stores the whole live config | Refused with no baseline backup, or with differences from the last one, unless `--accept-drift`. `sky backup` refuses the same differences, since a new backup would accept them. Pre-write backups don't count as a baseline |
| Weak supply during a flash write | Brown-out can interrupt it | Bus voltage must be inside the configured range (USB-only power is allowed) |
| Repeating a write that may have worked | Wears flash and hides the real state | Never retried; the read-back decides |
| Pausing or restarting the script with the engine running | The script owns throttle and kill | `sky stop` and `sky start` refuse if the motor is turning |

Blocking uses an allow-list: `check_packet()` in `safety.py` sees every packet, and any command not reviewed
and listed is refused. Only 10 of the 157 commands are allowed.

## The desktop app

The desktop app is a front end to the same engine, so everything on this page applies. It adds no firmware command and
no way around `check_packet()`. It adds these rules of its own, each covered by `tests/test_gui.py`:

| Risk | Protection |
|---|---|
| A live-values poll racing a write and taking its reply | One thread owns the port and runs one job at a time. Polling only happens with no job waiting |
| Polling resuming a paused script | Polling only sends `COMM_GET_VALUES` |
| Two writes queued by a double click | Only one exclusive job can be pending, and the controls and table are disabled meanwhile |
| Closing the window or disconnecting during a write | Refused until the job has finished |
| A confirmation clicked out of habit | YES has to be typed, and Enter does nothing until it is |
| Pausing with an engine the board can't see | Pausing needs a typed YES, stating the engine is stopped |
| The motor starting, or the config changing, during a dialog | `commit()` and the pause and start jobs check again after the dialog |
| The board disappearing during a dialog | Nothing is queued on a board that's gone, and the app says nothing was written |
| Forgetting that a script is paused | While connected, a banner stays up, including after a reconnect or restart, and disconnecting or closing warns about it |
| A lost reply hiding a pause | A pause counts as "may be paused" until a reply proves otherwise, and a timed-out pause stays pending in the firmware (`pause_eval`) |
| A REPL expression resuming a paused script | It needs a typed YES and an idle motor, as Start script does. Only a `> result` line clears the banner, and a failed expression turns it on, since its pause stays pending |
| Old numbers looking live | Tiles go blank and charts say "no data" when no value has arrived for 1.5 s |
| A new backup quietly accepting a script's temporary changes | Back up now shows the differences and needs a typed YES. A pre-write backup never counts as the baseline, so a failed restore can't accept them either |
| Choosing the wrong board | A chosen or typed port is never replaced, several boards are never guessed between, and the write dialog names the board's UUID |
| Another program taking the replies | The port is opened exclusively (`TIOCEXCL` on Linux, and Windows does this for every COM port), so VESC Tool can't open it afterwards |
| A packaged build leaving out its safety data | With no firmware word list the REPL refuses everything, and the Windows build must pass `--self-test` on the finished `.exe` before it's uploaded |
| A wedged USB link hanging the app | Serial writes time out after 2 s and become a communication error |
| Scrolling over a dropdown changing a setting | Dropdowns in the table ignore the mouse wheel |
| An override clicked past | The app has no `--allow-*` or `--force` override. They stay in the terminal, where each one is a typed flag |
| Mistaking the simulator for a real board | A permanent banner, and its backups go to a temporary folder |

The paused banner only knows what this app did. The board can't be asked, because `COMM_LISP_GET_STATS` resumes a paused
script. So a pause made by VESC Tool or `sky` doesn't show, and the banner can't see a power-cycle: it says "may be
paused" until you start the script or say you power-cycled it.

If USB stalls in the middle of a write, the firmware's parser (`packet_process_byte`) has no timeout, so it keeps
collecting bytes for the cut-off frame and the board can look dead. The app reports the state as unknown and doesn't
retry. A power-cycle clears it. Reaching a wrong command that way would need a CRC match inside the cut-off data.

Live values are sampled about 4 times a second, so a short fault can come and go unseen. A fault that is seen is kept in
the Console log, and the terminal command `faults` lists the firmware's own record.

### Updates

Git operations never touch the board, but they replace the code that does, so:

- Pull and reset need the board disconnected, including at the moment a reset is confirmed.
- Connecting is blocked while a pull or reset runs.
- If the app runs from that folder, a change made on this page blocks connecting until the app restarts, so the running
  checks are the ones in the folder. A pull made in a terminal isn't noticed, so restart the app yourself after one.
- Only a checkout of this project is accepted, as git itself sees it, because a reset in the wrong folder would destroy
  someone's work.
- Pull is `--ff-only`. Reset names the folder and shows every uncommitted change, local commit and untracked file it
  would replace, and needs a typed `RESET`. It refuses if the target or the folder changed since you reviewed it, keeps
  other untracked files, and prints how to get commits back. Uncommitted changes can't be brought back.
- Each pull and reset adds a line to `~/.local/share/sky-programmer/git_ops.log`.

## Every config write follows these steps

1. Read the config twice and require identical results.
2. Parse and range-check the values. Only the named fields' bytes change.
3. Check the result: validity rules, VESC Tool's ranges, board ratings and risky fields, that the supply stays in the
   new range, for BLDC to FOC that all three motor values were measured, and that the live config matches the baseline
   backup.
4. Show the changes and ask for confirmation that the engine is stopped. In the desktop app this is a typed YES.
5. Check the motor is idle and the supply is in range. This comes first, so a turning engine keeps its script.
6. Pause the script. If it doesn't pause, stop. If a later check refuses, say the script is now paused.
7. Check idle again, and re-read to require that nothing changed since planning.
8. Save a backup and read that back. If it fails, nothing is written.
9. Write **once**.
10. Read back and compare every field, allowing the one-step truncation the firmware applies to scaled values:
    - All as planned: done, and saved as the new baseline.
    - Only requested or firmware-managed fields differ: the firmware limited them, which is reported.
    - Unrelated fields changed: rolled back once, then reported.
    - Nothing changed: reported as failed.
    - No answer: reported as unknown, with the backup path. Not retried.

The read-back checks the board's working copy in RAM. Power-cycle and read again to prove the flash save.

## If a board stops responding

Try these in order. Steps 2 and 3 come from the firmware source and have not been tried on Flipsky boards yet.

1. **Power-cycle it** with the engine stopped, then run `sky status`.
2. **Script or C library crash:** the firmware doesn't start Lisp after a watchdog reset
   (`lispif_init`), so USB should stay usable. Fix or erase the script in VESC Tool.
3. **Config and script wipe:** connect HALL2 to SWCLK while powering up. The firmware resets the motor and app config
   and erases the stored Lisp and QML (`should_reset_config` in `main.c`). Afterwards use `sky restore` for the motor
   config; the app config and script have to go back through VESC Tool for now. Check that your board exposes these
   pins first.
4. **SWD:** an ST-Link on the board's SWD pads can reflash the firmware and bootloader.

**After a reset to defaults, keep the engine stopped.** A corrupted config boots on the firmware defaults, which on
Flipsky boards can mean FOC, and FOC boot calibration drives the phases. The same applies to the HALL2 wipe.

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
