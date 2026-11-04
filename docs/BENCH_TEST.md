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
| 15 | Run `sky status`, then the script with `sky start` | The script runs as before |

## C. Safety behaviour on the real board

| # | Action | Expect |
|---|---|---|
| 16 | Turn the motor by hand fast during `sky set l_temp_fet_start=84 --dry-run` | Refused if above about 200 erpm |
| 17 | `sky stop` with a script running, then check in `sky repl -e ":ctxs"` | Script paused |
| 18 | With a script that uses `conf-set` running: power-cycle, `sky backup` before it changes anything, then `sky set ...` | Reports the script's changes as drift |
| 19 | Unplug USB during `sky monitor` | Clear communication error, no hang |

## D. BLDC to FOC (only if you want FOC)

Do this with the engine stopped, since FOC start-up calibration drives the phases.

1. `sky set motor_type=FOC --dry-run`: refused if the FOC values were never measured.
2. Give the measured values for this board in the same command.
3. Check with `sky status`, then power-cycle and check again.
4. Go back with `sky set motor_type=BLDC`.

Please send back any step that didn't match, with its output. Steps 6, 7, 11 and 17 test assumptions the simulated
board can't confirm.
