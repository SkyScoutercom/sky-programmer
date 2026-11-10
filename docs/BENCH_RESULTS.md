# Bench results

The steps of [BENCH_TEST.md](BENCH_TEST.md), run on a real board.

## 2026-11-06 and 2026-11-07: Flipsky_110_100, firmware 7.00, Ubuntu

The board was in BLDC mode with the stored engine script, then `servo_74_test.lisp` for sections C and E. The engine was
stopped with the propeller clear throughout. Section B and most of C were run with `sky` from the `desktop-app` branch.

| Section | Result |
|---|---|
| A. Reading | Status, config and backup worked (steps 1 to 5 and 9). Steps 6 to 8 weren't reported |
| B. Writing a harmless field | **Pass.** Write verified, survived a power-cycle, restored, and the script restarted |
| C. Safety behaviour | **Pass**, except step 16, which can't be tested by hand (see below) |
| D. BLDC to FOC | Not run |
| E. Desktop app | **Pass** after one fix. Step 28 can't be tested by hand |
| F. Windows | Not run |

### What the bench found

| Found | Result |
|---|---|
| The app lost contact a few seconds after connecting | Another program had the port open without a lock, so both lost replies. The app now refuses to connect while another program of yours has the port, and names it. A udev rule stops ModemManager probing the board after plug-in |
| Unplugging USB was reported as "another program is reading this port" | pyserial gives the same error for both. The message now checks whether the port still exists |
| Typing a value into a Configuration cell crashed before the edit was staged | Qt reports the open text editor as the cell's widget. Nothing was written. Fixed, with a test that types into a cell |
| A script setting values at power-up looks like stored settings | `engine_foc_generator.lisp` sets the FOC model in RAM at every power-up, so the drift check reported it again after a power-cycle. The check was right; the message now warns about this case |

### Checks that worked on the real board

- Refusing to write while the start-up current read -4.46 A and -9.59 A, then allowing it once it settled.
- Refusing to write while a script's changes differed from the last backup.
- Pausing the script: messages stopped, `sky status` didn't restart it, and the servo held still at TAKEOFF.
- The paused banner came back after a reconnect and after a power-cycle, and the REPL asked before resuming.
- VESC Tool couldn't open the port while the app had it.

### Limits measured on the bench

- **Hand-turning can't be sensed.** Turning the engine by hand read 12 erpm at most, because at that speed the motor's
  voltage is too small to measure. So steps 16 and 28 can't be tested by hand. A running engine reads tens of
  thousands of erpm.
- **No motor temperature sensor.** It reads about -77 °C, so motor over-temperature protection can't work on this setup.
- **Channel 7 calibration has drifted.** TAKEOFF read about 2300 µs against the calibrated 2012 µs. It still decodes
  correctly.
- **Throttle servo:** 74° (a 1473 µs pulse) is the carburettor's full-throttle stop.

## 2026-11-09: section G, Lisp script upload (`stage-2-code-upload`)

Same board, firmware 7.00 at commit `20cbb362` (`release_7_00`). That commit is 4 behind `e877eae`, which the upload
code was checked against. Those 4 commits only touch an encoder driver, the Trampa 60_100 board files and the
packaging script, so the facts hold. The board started with `engine_control.lisp` plus its `hall-capture` native
library (40,145 bytes), stored by VESC Tool. Run with `sky` from the branch, engine stopped, propeller off.

| Step | Result |
|---|---|
| 35 `fw_info` | `20cbb362`, see above |
| 36 Backup | **Pass.** 40,145 bytes, 1 import: byte for byte `engine_control.lisp` (less its final newline) and `hall_capture.bin` plus a NUL |
| 37 Try | **Pass.** The board confirmed the stream, and the servo test ran |
| 38 Monitor | **Pass.** The script's messages showed; the ramp to 74° followed channel 7 |
| 39 Power-cycle | **Pass.** The stored engine script ran again (see the channel 7 note below) |
| 40–41 Store, start | **Pass.** 6,443 bytes stored and read back; started on request |
| 42 Power-cycle | **Pass.** The servo test ran by itself at power-up |
| 43 VESC Tool reads it | **Pass.** VESC Tool read the servo test from the board |
| 44 Cable pulled mid-store | **Pass**, twice. Pulled about 1 s after YES: before the erase, so nothing changed (`sky code backup` still found the servo test). Pulled about 3 s after YES: during the erase. The error named the old script's backup, and the board held no valid script |
| 45 Power-cycle with no script | **Pass.** It connected normally, with no fault and no script running |
| 46 Restore with a native library | **Pass.** All 40,145 bytes written and read back; it started, and the library reported hall rpm |

### What the bench found

| Found | Result |
|---|---|
| The engine script cranks the starter at power-up when channel 7 is at IDLE | It did so in steps 39 and 46: up to 36,000 erpm, and about 66 A against the engine. Section G now says to keep channel 7 at OFF before every power-cycle, start and restore. The motor noise dropped USB once (kernel `error -71`); the board itself didn't reset |
| A cable pulled before the erase reported only "USB read failed" | Nothing had been written, but the message didn't say so. It now says the stored script is unchanged |
| The erase takes a few seconds | A pull 3 s after YES still landed during the erase. It finished on the board, inside the 12 s the tool waits |
| `sky status` read -13 A at 0 erpm right after a power-up | One sample, then 0.05 to 0.08 A: the same start-up reading as in the 2026-11-07 run |

## 2026-11-10: section H, the desktop app's Scripts page (v0.1.4)

Same board and firmware, run from the app on Ubuntu after updating it to 0.1.4.

| Step | Result |
|---|---|
| 47 Back up stored script | **Pass** |
| 48 Try `servo_74_test.lisp` | **Pass** |
| 49 Store `servo_74_test.lisp` | **Pass** |
| 50 Restore the engine control script, with its `hall-capture` native library | **Pass.** The engine then ran on the restored script |

## 2026-11-10: section I, native libraries: the start-up guard and the rescue

Same board and firmware, `sky` from `stage-1` after PR #11, with the working `engine_control.lisp` and its
`hall-capture` native library. Engine stopped, propeller off, RC channel 7 at OFF. All steps were reported as working
as expected; no timings were sent back.

| Step | Result |
|---|---|
| 51 Back up the stored script | **Pass** |
| 52 Store `engine_control.lisp` with its native library and the start-up guard | **Pass** |
| 53 Power-cycle with the motor still: about 10 s with no script messages, then the engine script | **Pass** |
| 54 `sky code rescue` while the board powers up: backs up, then erases the script | **Pass** |
| 55 No valid script stored, and none runs after a power-cycle | **Pass** |
| 56 Store the engine script again and start it | **Pass** |

Not tested yet: the rescue against a library that really crashes (it waits for a hardware fallback: an ST-Link, or
the HALL2 jumper with a config backup), and a reset with the engine running, where the guard must not wait.

## 2026-11-10: storing with libraries built from C source (`sky code store --build`)

Same board and firmware, `sky` from `stage-1` after PR #14, Arm GNU Toolchain 13.3.rel1. Engine stopped, propeller
off, RC channel 7 at OFF.

| Check | Result |
|---|---|
| Build `hall-capture` from `code.c` in a temporary copy | **Pass.** 2528 bytes, SHA-256 `8ea0a38c…a24aa7d`, byte for byte the committed `.bin` and the library already on the board |
| Interface files against the board's `fw_info` | **Pass.** Both at `20cbb362` |
| Warnings | `set_pad_mode` (the library sets up its hall input pin) and the script's own `eeprom-store` calls, as expected |
| Store `engine_control.lisp` with the guard and the fresh build | **Pass.** 40,461 bytes stored and read back, then started |
