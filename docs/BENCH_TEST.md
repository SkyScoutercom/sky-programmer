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

Please send back any step that didn't match, with its output. Steps 6, 7, 11, 13, 15 and 17 test assumptions the
simulated board can't confirm.

**Don't test the boot-hang values on a real board.** There's no need: the app refuses them, and recovering takes SWD
or the HALL2 jumper.
