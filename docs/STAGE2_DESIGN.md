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
  reset. Recovery is the HALL2–SWCLK jumper at power-up, which erases the stored scripts, or an SWD programmer.
- So storing a script with a native library needs a separate, explicit confirmation that names this risk.
- A library's source should be checked for `write_nvm`, `wipe_nvm` and `store_cfg`: `wipe_nvm` erases the bootloader
  sector.

## Order of work

1. Lisp-only scripts: `backup`, `try`, `store`, `restore`, against a simulated board that follows the spec.
2. Independent review, then a bench test with `servo_74_test.lisp`.
3. Scripts with native libraries, once the recovery path on the Flipsky board is confirmed.
4. Building C libraries (`make`), with the source checks above.
5. The Scripts page in the desktop app.

## Open questions

The spec lists nine. The bench test on 2026-11-09 ([BENCH_RESULTS.md](BENCH_RESULTS.md)) answered two:
- Firmware commit: the board runs `20cbb362`, 4 commits behind `e877eae` with no change to the upload code.
- Erase time: a few seconds, inside the 12 s the tool waits.

Still open before native-library uploads (step 3):
- Whether the Flipsky 110100 exposes HALL2 and SWCLK for the recovery jumper, and whether an SWD programmer is at hand.
