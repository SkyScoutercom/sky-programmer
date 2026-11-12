# First bench test on a real board

Nothing here has been run on hardware yet, so work through this in order and stop at the first surprise.

**Setup:** engine stopped and disconnected or fuel off, propeller off, current-limited bench supply if you have one.
Close VESC Tool. Make a VESC Tool config backup first, as a second copy.

## A. Reading only (cannot change anything)

| # | Command | Expect |
|---|---|---|
| 1 | `sky status` | Correct board name, firmware 7.00, sensible bus voltage, correct mode |
| 2 | `sky get motor_type` | Matches VESC Tool |
| 3 | `sky get 'l_*'` | Limits match VESC Tool (compare at least `l_current_max`, `l_abs_current_max`) |
| 4 | `sky get --app app_to_use` | Matches VESC Tool |
| 5 | `sky monitor --seconds 5` | Values update; turning the motor by hand changes erpm |
| 6 | `sky term faults` | Prints the fault log. **Check it prints something** |
| 7 | `sky repl -e "(+ 1 2)"` | Prints a result containing 3 |
| 8 | `sky repl -e "(conf-store)"` | Refused. Exit code 2 |
| 9 | `sky backup` | Prints the file path |

## B. Writing a harmless field

Pick a value that doesn't matter on the bench.

| # | Command | Expect |
|---|---|---|
| 10 | `sky set l_temp_fet_start=84 --dry-run` | Shows one change, writes nothing |
| 11 | `sky set l_temp_fet_start=84` | Asks to confirm, then reports verified. **Note whether any field was reported as unexpected or limited** |
| 12 | `sky get l_temp_fet_start` | 84 |
| 13 | Power-cycle, then repeat 12 | Still 84, so it reached flash |
| 14 | `sky restore <the backup from step 9>` | One field changes back; verified |
| 15 | `sky status`, wait a few seconds, then `sky start` | The script stays paused after `sky status`, and runs as before after `sky start` |

## C. Safety behaviour on the real board

| # | Action | Expect |
|---|---|---|
| 16 | Turn the motor by hand fast during `sky set l_temp_fet_start=84 --dry-run` | Refused if above about 200 erpm |
| 17 | `sky stop` with a script running, then watch something it visibly does (servo, printed output) | It stops; `sky status` doesn't restart it |
| 18 | With a script that uses `conf-set` running: power-cycle, `sky backup` before it changes anything, then `sky set ...` | Reports the script's changes as drift |
| 19 | Unplug USB during `sky monitor` | Clear communication error, no hang |

## D. BLDC to FOC (only if you want FOC)

Do this with the engine stopped, since FOC start-up calibration drives the phases.

1. `sky set motor_type=FOC --dry-run`: refused if the FOC values were never measured.
2. Give the measured values for this board in the same command.
3. Check with `sky status`, then power-cycle and check again.
4. Go back with `sky set motor_type=BLDC`.

## E. Desktop app

Only after A to C pass. Start it with `sky-gui`, and use the same setup: engine stopped, propeller off.

| # | Action | Expect |
|---|---|---|
| 20 | Connect | Same board name, firmware and mode as `sky status`. Live values update |
| 21 | Turn the motor by hand | Speed changes on the tile and chart |
| 22 | Open Configuration | `l_current_max` and `l_abs_current_max` match VESC Tool |
| 23 | Change `l_temp_fet_start` to 84, then Review & write, then type YES | Paused banner appears. Reads back 84 |
| 24 | Power-cycle, reconnect | Still 84. The banner still says "may be paused", because the app can't see a power-cycle. Click "I power-cycled it…" |
| 25 | Backups: restore the backup from before step 23 | One field changes back |
| 26 | Unplug USB while connected | Clear message, back to the welcome screen, no hang |
| 27 | With VESC Tool connected, try to connect | Clear "cannot open" message |
| 28 | Pause script while turning the motor by hand fast | Refused above about 200 erpm |
| 29 | While connected here, try to connect VESC Tool | VESC Tool can't open the port |
| 30 | Pause script, type YES, then watch something the script visibly does | It stops, and the banner appears |
| 31 | With the script paused, disconnect and reconnect without power-cycling | The banner comes back, and a REPL line asks first |

## F. Windows

Windows has its own USB and serial behaviour, so a pass on Linux doesn't carry over. Install it, then repeat A to C with
`sky.exe` and E with the app, using the board's COM port. Check these as well:

| # | Action | Expect |
|---|---|---|
| 32 | Plug the board in with the app open | Its COM port appears within a few seconds. Bluetooth COM ports aren't listed |
| 33 | Power-cycle after a write, reconnect | The value is still there, as in step 13 |
| 34 | Pull the USB cable during live values | Clear message, back to the welcome screen, no hang |

## G. Uploading a Lisp script (stage 2)

Engine stopped, propeller off, and the throttle servo free to move: `servo_74_test.lisp` moves it. Use the Linux
terminal (`sky`), from `~/Desktop/Drone_ECU/vesc-starter`. The engine script can't be uploaded this way yet (it loads a
native library), so put it back with VESC Tool afterwards.

**Keep RC channel 7 at OFF before every power-cycle, `sky start` and restore.** At power-up the engine control script
runs its start sequence when channel 7 is at IDLE or TAKEOFF, and spins the starter even with no engine attached. On
the first bench run it reached 36,000 erpm and the motor noise dropped USB (kernel `error -71`). Move channel 7 to
TAKEOFF only on purpose, while the servo test is running.

| # | Action | Expect |
|---|---|---|
| 35 | `sky term fw_info` | Its Git hash. Send it back: the firmware facts were checked against `e877eae` |
| 36 | `sky code backup` | "Script backup saved (N bytes, 0 imported files)". Keep the path: it's the script on the board now |
| 37 | `sky code try servo_74_test.lisp`, type YES | A config backup line, then "Running from RAM". The servo does what the script does |
| 38 | `sky monitor --seconds 10` | The script's own messages, if it prints any |
| 39 | Power-cycle, reconnect | The script from step 36 runs again: `try` stored nothing |
| 40 | `sky code store servo_74_test.lisp`, type YES, then answer no to starting it. Time it | "Stored and read back: all ... bytes match", then "not running". Send back how long it took |
| 41 | `sky start`, type YES | The servo test runs |
| 42 | Power-cycle, reconnect | The servo test runs by itself at power-up |
| 43 | Optional: open VESC Tool, Lisp page, read the code from the board | The same text as `servo_74_test.lisp` |
| 44 | Run step 40's command again, and pull the USB cable while it writes | An error naming the old script's backup. Reconnect: `sky code backup` says no valid script is stored (if the store finished before the pull, say so and try again) |
| 45 | Power-cycle, reconnect | The board connects normally, and no script runs |
| 46 | `sky code restore <path from step 36>`, type YES, answer YES to start | The original script is back and running |

## H. The Scripts page in the desktop app

The same engine as section G, so this only checks the page. Same precautions: engine stopped, propeller off, **RC
channel 7 at OFF** before every store, restore, start and power-cycle.

| # | Action | Expect |
|---|---|---|
| 47 | Scripts page: **Back up stored script** | A dialog with the size and imported files, and a new row in the list |
| 48 | **Try a file…**, pick `servo_74_test.lisp`, type YES | "Script running". The servo test's messages appear in the Console |
| 49 | **Store a file…**, pick `servo_74_test.lisp`, type YES, then cancel the start question | "Script stored": all bytes match. The paused banner shows, and the new script isn't running |
| 50 | Select the step 47 backup, **Restore…**, answer the native-library question, type YES, then start it | The engine script is back and running. Send back each dialog's wording that was unclear |

## I. Native libraries: the start-up guard and the rescue, on a healthy board

No crashing library here: this checks the guard and the rescue with the working `engine_control.lisp`. Terminal, from
`~/Desktop/Drone_ECU/vesc-starter`, engine stopped, propeller off, **RC channel 7 at OFF** throughout. Check first that
`engine_control.lisp` on disk is the version you want on the board: it was changed after the board's copy was stored.

| # | Action | Expect |
|---|---|---|
| 51 | `sky code backup` | Keep the path: the script on the board now |
| 52 | `sky code store engine_control.lisp`, YES to the native-library question, YES to the engine question, no to starting | "Native libraries: hall-capture (2528 bytes)", then "Stored and read back". The stored script starts with the guard |
| 53 | Power-cycle with the motor still, then `sky monitor --seconds 25` | No script messages for about 10 s after the board answers, then the engine script's own messages. Send back roughly how long the quiet part was |
| 54 | `sky code rescue --engine-stopped`, then power-cycle the board while it waits | "Found …", then "Erased" within a few seconds of the board appearing |
| 55 | `sky code backup`, then power-cycle and `sky monitor --seconds 15` | No valid script is stored, and no script messages appear |
| 56 | Step 52 again, answering YES to starting it. Wait 10 s | The engine script is back and running |

## J. The desktop app: rescue and build from source

Same precautions: engine stopped, propeller off, **RC channel 7 at OFF**. Have `engine_control.lisp` stored with its
guard (section I) first.

| # | Action | Expect |
|---|---|---|
| 57 | Scripts page: tick **Build from source**, **Store a file…**, pick `engine_control.lisp`, answer the questions, then cancel the start question | The native-library question lists `hall-capture` with "matches this build byte for byte" and the `set_pad_mode` warning, then "Script stored" |
| 58 | Disconnect. On the start screen, **Rescue a boot-looping board…**, type YES, then power-cycle the board | The rescue window shows "Found …", "Backed up its script first …", then the app says "Rescued" |
| 59 | Connect, Scripts page, **Back up stored script** | "No valid script is stored". The list also shows the "pre-rescue" backup |
| 60 | Step 57 again, starting it at the end | The engine script is back and running |

## K. The board on its UART instead of USB

The firmware answers the same commands on its UART as on USB, so everything here works the same, at 115200 baud.
The aim is a link that keeps working with the engine running, where USB drops out.

**Hardware:** a **3.3 V** USB-to-UART adapter, ideally an **isolated** one (or a USB isolator in front of a normal
one), so the laptop's ground isn't tied to the drone's while the generator runs. On the VESC's COMM port, TX and RX
are free (see `WIRING.md` in vesc-starter, and check its pin order with a meter first):

| Adapter | VESC COMM port |
|---|---|
| TX | RX/SDA |
| RX | TX/SCL |
| GND | GND |
| 5 V / 3.3 V out | **not connected** |

Keep the TTL wires short, with the adapter near the VESC. Engine stopped, propeller off, RC channel 7 at OFF.

| # | Action | Expect |
|---|---|---|
| 61 | With the board on USB: `sky get --app app_to_use`, `sky get --app permanent_uart_enabled` and `sky get --app app_uart_baudrate` | UART works when `app_to_use` is one of `APP_UART`, `APP_PPM_UART` or `APP_ADC_UART`, or `permanent_uart_enabled` is true; the baud rate is 115200. If not, change it in VESC Tool (App Settings → General) and write the app config: this tool can't write app config. Don't choose a PPM app: the servo pin is an output |
| 62 | Unplug the VESC's USB. Plug in the adapter. `sky status` | It finds `/dev/ttyUSB0` by itself and shows the same board, firmware and mode as on USB. With the USB board and the adapter both plugged in, it asks for `--port` instead of guessing |
| 63 | `sky monitor --seconds 10` | Values update, and the script's messages arrive |
| 64 | `sky backup`, then `sky set l_temp_fet_start=84`, then `sky restore <that backup>` | Verified write and restore, as over USB (steps 9–14) |
| 65 | `sky code backup`, then `sky code restore <that backup> --start` | The engine script is stored again over UART and read back; it runs |
| 66 | `sky code rescue --engine-stopped`, then power-cycle the board | Over UART the port doesn't disappear on a reset: the rescue keeps trying until the board answers, then "Found …", "Backed up its script first …", "Erased" |
| 67 | `sky code restore <the pre-rescue backup> --start` | The engine script is back and running |
| 68 | Desktop app: pick `/dev/ttyUSB0` in the port list, Connect | Same as over USB |
| 69 | Later, with the engine running and the full precautions of `docs/RESET_TEST_PLAN.md`: `sky monitor --seconds 30` over the adapter | **The question this section is for:** does the UART link keep working with the ignition running? Note any "no reply" errors. If it holds, stages 1 and 2 of the reset test become possible over UART |

Please send back any step that didn't match, with its output. Steps 6, 7, 11, 13, 15 and 17 test assumptions the
simulated board can't confirm.

**Don't test the boot-hang values on a real board.** There's no need: the app refuses them, and recovering takes SWD
or the HALL2 jumper.
