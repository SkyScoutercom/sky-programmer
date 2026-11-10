# Stage 2 design: uploading scripts and native libraries

Status: step 1 (Lisp-only scripts) built and tested against the simulated board in `src/sky_programmer/code.py`;
not yet bench-tested. The rest is design. The firmware facts behind each rule are in [LISP_UPLOAD_SPEC.md](LISP_UPLOAD_SPEC.md), with
file and line citations into firmware 7.00 and VESC Tool.

## Commands

| Command | What it does |
|---|---|
| `sky code backup` | Read the stored script and its imports (`COMM_LISP_READ_CODE`) into a file, with a checksum |
| `sky code try script.lisp` | Stream the script to RAM (`COMM_LISP_STREAM_CODE`, mode 1). Nothing is stored; a power-cycle brings back the stored script |
| `sky code store script.lisp` | Pack, back up, erase, write, read back, then ask before starting it |
| `sky code restore <backup>` | Store a backup the same way |

The desktop app gets a Scripts page doing the same through the same engine.

## Rules

**Packets** (enforced in `check_packet`, which sees every packet):
- `COMM_LISP_WRITE_CODE` only through the upload path. Payload of at least 6 bytes (ID, 4-byte offset, data), and
  `0 ≤ offset` and `offset + length ≤ 8 + N ≤ 131,072`. The firmware checks neither, and a bad offset reaches the
  bootloader sector.
- `COMM_LISP_READ_CODE`: length 1 to 400, offset 0 or more. The firmware's check is signed.
- `COMM_LISP_ERASE_CODE`: exactly the command plus 4 bytes, only through the upload path.
- `COMM_LISP_STREAM_CODE`: only through `try`. The chunk at offset 0 is never retried, because it restarts LispBM.

**Upload order:**
1. Check the motor is idle, and ask for the typed engine-stopped `YES`: a stored script starts at every boot, and every
   erase and write releases the motor for 5 s.
2. Back up the stored script. A failed backup stops the upload before anything is erased.
3. Save a config backup, because the new script may change config in RAM when it starts.
4. Erase once, with a timeout of at least 8 s. Erase stops the running script, even if it then fails.
5. Write each chunk once, checking the offset echoed in every reply. Never retry a write.
6. Read every byte back and compare. A non-empty read-back also proves the firmware's own checksum passed.
7. Only then ask before starting it with `SET_RUNNING(1)`, which restarts the script from the beginning.

If anything fails after the erase, the board holds no valid script. It still boots, with no script running, and the
backup can be restored.

**Content checks** before a script is tried or stored:
- Refuse `image-save`, which would make the board boot a saved `main` instead of the stored script, plus the words the
  REPL already blocks for writing flash or reaching other boards.
- Refuse `conf-store`, `conf-restore-mc`, `conf-restore-app` and `store-backup`: `conf-set` takes any value unchecked,
  and storing one such as a bad switching frequency can hang the board at every boot. Stored config goes through
  `sky set`.
- Refuse the words that build a symbol from data at run time (`str2sym`, `u2sym`, `unflatten`, the `read` family):
  `apply` on such a symbol calls it, which would get past every word check. The REPL also blocks `apply`. The word
  lists are a guard against mistakes, not a sandbox: a script runs with full access to the board.
- Refuse non-STM32 hardware (the hardware-type byte in `COMM_FW_VERSION` must be 0): the code area is only known there.
- `try` reports success only after the board prints `Stream done, starting...`. After a stream that broke off, the
  firmware can answer a new first chunk with the old result and run nothing (`lispif.c`, `offset_last`).
- The firmware only erases the area when its first byte isn't blank, so old data behind a blank first byte can spoil a
  store. The read-back catches it, and a second store works.
- Resolve every `(import "path" 'name)` from the script's folder, as VESC Tool does, and refuse a missing file.
- Size: at most 131,064 bytes from byte 8, as the firmware requires.

**Native libraries** (a `.bin` loaded with `load-native-lib`):
- They can't be tried first: streaming ignores `import`, so a library's first run is always from flash.
- A library that crashes at start-up can make the board reboot over and over, because a hard fault isn't a watchdog
  reset. The hardware recoveries are the HALL2–SWCLK jumper at power-up, which erases the stored scripts **and resets
  the motor and app config to defaults** (`main.c`, `should_reset_config`), or an SWD programmer.
- So storing a script with a native library needs a separate, explicit confirmation that names this risk.
- **Start-up guard and rescue (built, not yet tried with a crashing library).** The firmware binds a script's imports
  before it runs, but only runs a library when `load-native-lib` is evaluated (`lispif.c`, `lispif_c_lib.c`). USB
  answers commands, the erase among them, in its own thread while the script sleeps (`comm_usb.c`), and the script
  starts about 0.5 s after USB (`commands.c` waits for `main` to finish). So `sky code store` puts
  `(sleep 10)` at the top of every script that uses native libraries, and `sky code rescue` waits for the board to
  appear, waits 2 s for the script to start, then erases it (`lispif_stop` first). The next boot runs no script
  (`lispif_restart`). It can't help once a library has loaded and wedged the board (an `init` that never returns, or
  a `stop` function that faults), but the window comes before the load at every boot.
- An EEPROM boot counter was considered and dropped: every EEPROM write releases the motor for 5 s
  (`conf_general.c`), so clearing the counter while the engine runs isn't safe.
- Packing matches VESC Tool's `lispPackImports` byte for byte for ASCII import names (non-ASCII names are refused:
  VESC Tool sizes its table by characters but writes UTF-8). A test repacks the real board's backup to prove it.
- The guard only waits while `(get-rpm)` is under 200 erpm: after a reset with the engine running, the engine script
  must take over again within about a second. BLDC tracks a released, spinning motor's rpm (`mcpwm.c`, back-EMF in
  `MC_STATE_OFF`), and the rescue refuses a turning motor anyway.
- A native library is unchecked machine code: it can call `commands_process_packet`, `wipe_nvm` and `write_nvm`
  (`vesc_c_if.h`). So `load-native-lib` may only name one of the script's imports directly, never an array built at
  run time, and each library is shown with its SHA-256 before the second YES.
- A library's source should be checked for `write_nvm`, `wipe_nvm` and `store_cfg`: `wipe_nvm` erases the bootloader
  sector.

## Order of work

1. Lisp-only scripts: `backup`, `try`, `store`, `restore`, against a simulated board that follows the spec.
2. Independent review, then a bench test with `servo_74_test.lisp`.
3. Scripts with native libraries: built with the start-up guard and `sky code rescue`. Bench section I tries the
   rescue on a healthy board; the deliberate-crash test waits for a hardware fallback (an ST-Link, or the HALL2
   jumper with a config backup).
4. Building C libraries: `sky code build` and `sky code store --build` (`native.py`). They build in a temporary copy
   with the VESC rules and the Arm GNU Toolchain 13.3.rel1, refuse `commands_process_packet`, `write_nvm`, `wipe_nvm`,
   `store_cfg`, `store_backup_data` and `can_send_buffer` in the source, check the ELF needs no relocation (no GOT,
   `.data` or `.bss`, only `init` global), require the interface files' commit to match the board's `fw_info`, and
   report whether the committed `.bin` matches the fresh build. The source check reads the text only: it catches
   mistakes, not intent.
5. The Scripts page in the desktop app: built for Lisp scripts (`gui/scripts_page.py`), on the same engine; bench section H.

## Open questions

The spec lists nine. The bench test on 2026-11-09 ([BENCH_RESULTS.md](BENCH_RESULTS.md)) answered two:
- Firmware commit: the board runs `20cbb362`, 4 commits behind `e877eae` with no change to the upload code.
- Erase time: a few seconds, inside the 12 s the tool waits.

Still open before native-library uploads (step 3):
- Whether the Flipsky 110100 exposes HALL2 and SWCLK for the recovery jumper, and whether an SWD programmer is at hand.
