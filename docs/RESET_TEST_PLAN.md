# Plan: a board reset with the engine running

Status (2026-11-12): **stage 0 passed** (results below and in [BENCH_RESULTS.md](BENCH_RESULTS.md)). **Stages 1 and
2 can't be run as written:** with the engine running, its ignition noise drops USB, so neither the reset (VESC Tool's
Reboot) nor the monitor can reach the board. The guard's behaviour after a reset with the engine running is
therefore checked against the firmware source, not measured. See "A way to measure it later" for a test without the
ignition.

## What it proves

A brown-out or EMI reset in flight restarts the VESC while the engine keeps running. Two things must then hold:

1. **The start-up guard doesn't wait.** Scripts stored by the SKY programmer begin with
   `(if (< (abs (get-rpm)) 200) (sleep 10))`. With the engine turning the generator, `get-rpm` must already read far
   above 200 erpm when the script starts, so the script runs at once instead of 10 s later.
2. **The engine script takes over again without a new start.** `engine_control.lisp` must not crank the starter on a
   running engine, and in TAKEOFF it must return to regulating.

## What the firmware does (firmware 7.00, checked in the source)

| Reset | Examples | Script afterwards |
|---|---|---|
| Normal reset | Power dip (brown-out), hard fault, `COMM_REBOOT` (VESC Tool's Reboot, `commands.c:695-697`) | Runs, after the boot below |
| Watchdog reset | The motor control loop stalls, a library thread that won't stop, the terminal's `rebootwdt` | **Doesn't run at all until the next power-cycle** (`lispif.c:90-96`). See "Found while planning" |

**Boot time** in BLDC mode, from the reset to the script's first line: about 3 s. That's 0.1 s of start-up, about
1 s of current-sensor calibration (`mcpwm.c`), USB held off for 1.5 s (`comm_usb_serial.c`), then 0.5 s before Lisp
starts (`main.c`, `commands.c`). **So the engine runs without the VESC for about 3 s after any reset, guard or not.**
With the guard waiting, it would be about 13 s; this test tells the two apart.

**Rpm while released:** BLDC keeps tracking the rotor from its back-EMF when no switches are driven (`mcpwm.c`,
`MC_STATE_OFF`). The step 46 bench log showed it: after "starter released", the readings went 18,932 → −4,234 →
decaying.

**`engine_control.lisp` after a reset** (read 2026-11-12; check again if the script changes):
- `armed` starts false (line 547), and only OFF on channel 7 arms a start. A reset while channel 7 stays at IDLE or
  TAKEOFF therefore doesn't start a crank.
- Even when armed, a start first checks the hall rpm, and prints "start: engine already running, starter not used"
  (lines 589-590).
- In TAKEOFF with the engine running, it goes back to regulating (line 610).
- Channel 7 is debounced for 0.4 s after the script starts, and `hall-capture` reports rpm after one full
  revolution, so the hall rpm is there by the time it's checked.

## Risks during the 3 s without the VESC, and how the plan covers them

| Risk | Cover |
|---|---|
| No throttle control: the servo gets no pulses | Stage 0 watches what the servo does during a reset, with the engine off. Only continue if it holds or goes to idle |
| No overvoltage, overspeed or temperature cuts in the script | The battery stays on the bus to absorb the generator. A meter on the bus, and the abort limits below |
| The VESC calibrates its current sensors at boot while the generator spins, which could leave wrong current readings | Compare `motor_a` at idle before and after the reset. A large offset is an abort and a finding |
| Nobody can stop the engine through the VESC during the reset | **An engine kill (ignition cut) that doesn't depend on the VESC, held by a second person, tested before the engine starts** |

## Before the day

- [ ] Engine secured on its test stand. No propeller or other exposed rotating part; everyone clear of the plane of
      rotation.
- [ ] Independent engine kill wired and tested: with the engine running, it stops it. A second person holds it
      throughout.
- [ ] Fire extinguisher at hand; only the fuel the test needs.
- [ ] Battery connected to the bus as in flight. Bus voltage meter visible. Write down the normal idle bus voltage.
- [ ] `sky backup` and `sky code backup` taken, with the board just powered up and the engine stopped.
- [ ] `engine_control.lisp` stored by the SKY programmer (it starts with the guard): check with `sky code backup`
      and the start of the saved text.
- [ ] On the laptop: VESC Tool installed (it's the reset trigger), and a terminal ready to run the monitor.
- [ ] Phone video of the servo, the bus meter and the laptop screen, for timing.

**Abort at once (engine kill)** if any of these happens:
- the bus voltage goes above the board's `l_max_vin`, or above the script's 50.3 V cut;
- the engine speeds up on its own;
- the starter engages on the running engine;
- any fault code, smoke or a smell;
- the board doesn't come back on USB within 10 s of the reset;
- `motor_a` at idle reads more than a few amps away from its value before the reset.

## Stage 0: engine OFF, no fuel (dry run)

| # | Action | Expect |
|---|---|---|
| R1 | Channel 7 at OFF. Connect VESC Tool, then press its **Reboot** | VESC Tool loses the board and the board comes back on USB in about 3 s. Note how VESC Tool behaves: you'll need to close it right after Reboot in later steps |
| R2 | Keep channel 7 at **OFF** (OFF → IDLE would arm a start and crank). The servo already sits at its no-throttle position (about 801 µs) in OFF. Reboot again from VESC Tool and watch the servo | **Write down what the servo does during the reset:** holds, goes limp, or moves. Stop here if it moves towards full throttle |
| R3 | Close the SKY programmer app. Connect VESC Tool, start the monitor loop below, then press Reboot and close VESC Tool | The monitor connects when the board comes back. With the motor still, the guard waits: the script's first message comes about 10 s after the monitor starts. That's the baseline |

The monitor loop waits for the board to come back, then records 40 s. Save it as `reset_watch.sh` and run
`sh reset_watch.sh` (type the command: copying it from a terminal window can pick up stray border characters):
```sh
until sky status >/dev/null 2>&1; do sleep 0.2; done
sky monitor --seconds 40 --log reset_$(date +%H%M%S).csv
```
On Windows, as `reset_watch.bat` in the SKY terminal:
```bat
@echo off
:wait
sky status >nul 2>&1 || (timeout /t 1 >nul & goto wait)
sky monitor --seconds 40 --log reset.csv
```

### Stage 0 results (2026-11-12)

| # | Result |
|---|---|
| R1 | **Pass.** The board rebooted and came back on USB. VESC Tool didn't reconnect by itself |
| R2 | **Pass.** The servo went limp while the board restarted, then went back to no throttle. It didn't move towards open |
| R3 | **Pass.** With the motor still, the first script lines came 9.4 s after the board answered: `hall_capture` loading (so the library loads only after the guard), then at 10.6 s `hall clock OK`, `engine control ready: switch ch7 OFF, then IDLE to start` and `RC ch7: OFF (902 us)`. No crank, no fault, bus steady at 45.8 V. `motor_a` moved from about 0.01 A to about −0.1 A when the script started |

## Stage 1: engine running at IDLE (not possible as written)

**Not possible as written:** the engine's ignition noise drops USB, so VESC Tool can't send the Reboot and the
monitor can't read the board. Kept for reference, and for a setup that keeps USB working with the engine running.

Only after stage 0 passed and the checklist is complete. Channel 7 at OFF, engine started the normal way (OFF →
IDLE), warm, steady at idle. Battery on the bus, kill held.

| # | Action | Expect |
|---|---|---|
| R4 | Run `sky monitor --seconds 10` | Note idle rpm, bus voltage and `motor_a` before the reset. Close it |
| R5 | Start `bash reset_watch.sh`. Connect VESC Tool and press **Reboot**, then close VESC Tool at once | The engine keeps running. The bus voltage stays within limits |
| R6 | Watch the monitor | Script messages within about 1–2 s of the monitor starting, **not** after 10 s. No "start: attempt"; if the start check runs, it says "engine already running, starter not used". Rpm, bus voltage and `motor_a` back to what R4 showed |
| R7 | Channel 7 to OFF; the engine stops the normal way | The script stops the engine as usual |

**Pass:** R6 shows no 10 s wait and no crank, and nothing hit an abort limit.

## Stage 2: engine running in TAKEOFF (not possible as written, like stage 1)

Same as stage 1, with channel 7 at TAKEOFF and the engine regulating before the reset. **Pass:** after the reset the
script prints that it's regulating again, the generator output recovers, and nothing hit an abort limit. This is the
in-flight case, at a higher speed and load; decide on the day whether to run it.

## Found while planning

- **After a watchdog reset the engine script doesn't run until the next power-cycle** (firmware 7.00,
  `lispif.c:90-96`). In flight, the engine would keep running with no VESC control: no throttle regulation, no cuts.
  This is firmware behaviour, not the SKY programmer's, and the guard doesn't change it. It's worth deciding how the
  drone should handle it (for example a watchdog on the flight controller's side that cuts the engine if the
  generator stops reporting).
- **The engine runs without the VESC for about 3 s after any reset**, even without the guard. The earlier estimate
  of "about a second" was wrong; the boot time above is from the firmware source.
- **The engine's noise drops the laptop's USB link, but not the board.** When USB dropped with the starter and
  engine running (bench step 39, 2026-11-09), the kernel logged USB protocol errors while the script kept running.
  The noise reaches the USB link, not the board's processor, which makes an in-flight VESC reset less likely than
  the USB drops suggest.

## A way to measure it later (optional)

Turn the engine with an electric drill on the crankshaft, **ignition off and spark plug out**, so there's no
ignition noise and USB stays up. The generator then spins without the engine running. With the board connected to
VESC Tool, start `reset_watch.sh`, press Reboot while the drill turns the engine, and close VESC Tool. **Pass:** the
script's first lines (`hall_capture: …`) come within about 1 s of "Board answered", not after about 10 s. A typical
drill speed gives thousands of erpm, far above the guard's 200. Whether the drill can be coupled safely depends on
the engine; plan it like stage 1, with the propeller off and people clear.
