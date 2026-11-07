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
