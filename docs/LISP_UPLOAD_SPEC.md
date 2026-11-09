# Stage 2 spec: LispBM script and native-library upload (firmware 7.00)

Research only. Nothing was sent to any board and no repo code was changed.

## 0. Sources and how to read the citations

Citations name files in the two repos below, at the commits listed (`fw/` for the firmware, `vt/` for VESC Tool).

| Repo | Ref used | Head commit at fetch time |
|---|---|---|
| vedderb/bldc (firmware) | `release_7_00` branch | `e877eaead23e23cc0e160302d75554eaeb2c98d0` (2026-11-05) |
| vedderb/vesc_tool | `release_7_00` branch (it exists, so master was **not** used) | `6528c4e2a963e86ae4104737ddf7c76ffc987d84` (2026-07-01) |

Citation prefixes:

- `fw/X:n` = `stage2_src/X` (firmware). Repo paths: `comm/commands.c`, `flash_helper.c/.h`, `lispBM/lispif.c`,
  `lispBM/lispif_vesc_extensions.c`, `lispBM/lispif_c_lib.c`, `lispBM/lispBM/src/lbm_image.c`,
  `lispBM/lispBM/src/lbm_c_interop.c`, `lispBM/lispBM/src/symrepr.c`, `lispBM/lispBM/src/heap.c` (saved as
  `lbm_heap.c`), `main.c`, `timeout.c`, `irq_handlers.c`, `motor/mc_interface.c`, `motor/mcpwm.c`, `terminal.c`,
  `comm/packet.h`, `util/buffer.c`, `util/utils_sys.c`, `datatypes.h`. `mcpwm_foc.c` is cited from the earlier
  download in `tmp/fw/`.
- `vt/X:n` = `stage2_src/vesc_tool/X` (VESC Tool): `codeloader.cpp/.h`, `commands.cpp`, `pages/pagelisp.cpp`
  (`pagelisp.cpp`), `pages/pagelisp.ui`, `vbytearray.cpp`, `packet.cpp`, `datatypes.h`.

The earlier `tmp/fw/lispif_vesc.c` is byte-identical to today's `lispBM/lispif_vesc_extensions.c`; `tmp/fw/lispif.c`
and `tmp/fw/packet.c` are identical to today's files. The branch moves, so the board's own build may differ (see
open questions).

Labels used below: **[source]** = read in the code at the cited line. **[inference]** = my reasoning from that code,
not something a line states.

All multi-byte integers are big-endian: `buffer_get_uint32` (fw/buffer.c:178-185); VESC Tool's `vbAppend*` match.
Packet framing and CRC are what `protocol.py` already does; the stored-code CRC uses the same `crc16()`
(fw/flash_helper.c:30,483; vt/codeloader.cpp:376; vt/packet.cpp:26,95).

---

## 1. Commands, IDs and byte layouts

### 1.1 Command IDs [source]

From `COMM_PACKET_ID` in fw/datatypes.h:1099-1111,1129 (VESC Tool's vt/datatypes.h:998-1009,1027 agree):

| ID | Name | Handled in |
|---|---|---|
| 130 | `COMM_LISP_READ_CODE` | fw/commands.c:1387-1430 (shares the case with `COMM_GET_QML_UI_APP`) |
| 131 | `COMM_LISP_WRITE_CODE` | fw/commands.c:1454-1473 (shares with `COMM_QMLUI_WRITE`) |
| 132 | `COMM_LISP_ERASE_CODE` | fw/commands.c:1432-1452 (shares with `COMM_QMLUI_ERASE`) |
| 133 | `COMM_LISP_SET_RUNNING` | fw/lispif.c:187-202 |
| 134 | `COMM_LISP_GET_STATS` | fw/lispif.c:205-282 |
| 135 | `COMM_LISP_PRINT` | board-to-host text only (fw/commands.c:1752) |
| 138 | `COMM_LISP_REPL_CMD` | fw/lispif.c:284-490 |
| 139 | `COMM_LISP_STREAM_CODE` | fw/lispif.c:492-640 |
| 152 | `COMM_LISP_RMSG` | fw/lispif.c:642-644 (not part of upload) |

133, 134, 138, 139 and 152 are passed from `commands_process_packet` to `lispif_process_cmd`
(fw/commands.c:1602-1611). 130-132 are handled directly in `commands.c`. None of them is a "blocking" command, so none
runs in the `comm_block` thread (that list is fw/commands.c:1684-1716). [source]

### 1.2 `COMM_LISP_ERASE_CODE` (132)

| Direction | Bytes |
|---|---|
| Request (VESC Tool) | `[132][int32 size]` (vt/commands.cpp:2226-2232) |
| Firmware reads | nothing after the ID; `size` is ignored (fw/commands.c:1432-1452) [source] |
| Reply | `[132][uint8 ok]`, ok = 1 if the erase of the Lisp code sector returned `FLASH_COMPLETE` (fw/commands.c:1447-1451) |

What it does, in order [source]:
1. `lispif_stop()` (fw/commands.c:1440): stops native libraries, then kills the evaluator (fw/lispif.c:676-692).
2. `flash_helper_erase_code(CODE_IND_LISP_CONST)`: sector 8, the LispBM image and constant heap
   (fw/commands.c:1441). **Its result is discarded.**
3. `flash_helper_erase_code(CODE_IND_LISP)`: sector 10, the stored script (fw/commands.c:1445). Only this result is
   reported.

### 1.3 `COMM_LISP_WRITE_CODE` (131)

| Direction | Bytes |
|---|---|
| Request | `[131][uint32 offset][data...]` (vt/commands.cpp:2206-2213; fw/commands.c:1456-1463) |
| Reply | `[131][uint8 ok][uint32 offset]`, the offset echoed from the request (fw/commands.c:1467-1472) |

The firmware writes `len - 4` bytes to `0x080C0000 + offset` (sector-10 base + offset) one byte at a time
(fw/commands.c:1462-1463; fw/flash_helper.c:210-214, 438-470). There is **no bounds check** on `offset` or length
anywhere on this path. See section 3.3.

The offset is relative to the start of the sector, so it includes the 8-byte header in 2.1. VESC Tool sends 384 data
bytes per packet (vt/codeloader.cpp:435), so the payload is 389 bytes. `PACKET_MAX_PL_LEN` is 512 unless the hardware
config overrides it (fw/packet.h:27-28).

### 1.4 `COMM_LISP_READ_CODE` (130)

| Direction | Bytes |
|---|---|
| Request | `[130][int32 len][int32 offset]` (vt/commands.cpp:2197-2204; fw/commands.c:1391-1392) |
| Reply, code valid | `[130][int32 total_len][int32 offset][len bytes]` (fw/commands.c:1421-1429) |
| Reply, no valid code | `[130][int32 0][int32 0]` (fw/commands.c:1407-1415) |
| Reply, out of range | **none**, if `len + offset > total_len` or `len > PACKET_MAX_PL_LEN - 10` (fw/commands.c:1417-1419) |

- `offset` is relative to the code start (sector byte 8). `total_len` is the stored length N from the header. The
  8-byte header itself cannot be read (fw/flash_helper.c:216-224, 230-240). [source]
- "Valid" means the stored CRC matched (`qmlui_check`, fw/flash_helper.c:472-490). A reply with `total_len > 0`
  therefore means the firmware has checked the CRC of the whole stored blob since its last write. [source + inference]
- VESC Tool reads 10 bytes at offset 0 first, then chunks of up to 400 bytes, with 5 tries and 1.5 s each
  (vt/codeloader.cpp:556-580). The firmware allows up to 502 bytes when the limit is 512.
- `len` and `offset` are signed and only the sum is checked, so a negative value passes into `memcpy`
  (fw/commands.c:1417, 1426). [source] A negative `len` would probably crash the board. [inference]

### 1.5 `COMM_LISP_SET_RUNNING` (133)

| Direction | Bytes |
|---|---|
| Request | `[133][uint8 running]` (vt/commands.cpp:2234-2240) |
| Reply | `[133][uint8 ok]` (fw/lispif.c:197-201) |

- `0`: `pause_eval(0, 2000)` only pauses the evaluator (fw/lispif.c:191-192, 157-176). It does not call
  `lispif_stop_lib`. [source] Native-library threads and callbacks therefore keep running while the script is
  "paused". [inference]
- `1`: `lispif_restart(true, true, true)` (fw/lispif.c:194), a **full restart from the start of the script, not a
  resume**. If there is no valid stored code, nothing happens, it replies 0, and the current evaluator stays as it was
  (fw/lispif.c:704-707, 844). [source]

### 1.6 `COMM_LISP_STREAM_CODE` (139)

| Direction | Bytes |
|---|---|
| Request | `[139][int32 offset][int32 tot_len][int8 mode][code bytes...]` (vt/commands.cpp:2215-2224; fw/lispif.c:496-499) |
| Reply | `[139][int32 offset][int16 result]` (fw/lispif.c:514-517, 623-624, 639) |

Quirk [source]: on the duplicate, not-running, error and tokenizer-invalid paths, the reply length is the request parse
index `ind` (9) rather than `send_ind` (7). Those replies carry 2 uninitialized trailing bytes (fw/lispif.c:521, 531,
552, 565, 578, 590). Parse only the first 7 bytes.

Result codes (fw/lispif.c):

| Result | Meaning | Line |
|---|---|---|
| 0 | chunk accepted | 623 |
| -1 | Lisp not running, even after the restart attempt | 527-532 |
| -2 | previous stream's reader didn't close within 1500 ms | 536-554 |
| -3 | couldn't pause the evaluator within 1000 ms | 559-567 |
| -4 | couldn't start eval | 572-580 |
| -5 | channel write timed out (1500 ms) | 626-630 |
| -6 | reader closed (parser stopped consuming) | 631-636 |
| -15 | tokenizer invalid, e.g. a chunk without offset 0 first | 586-592 |

Mode is used only when `offset == 0` (fw/lispif.c:504-512):

| Mode | VESC Tool label (vt/pagelisp.ui:358-372, default index 1) | Lisp not running | Lisp running |
|---|---|---|---|
| 0 | "Load on top" | restart **without** stored code | nothing; streamed code runs alongside the stored script |
| 1 | "Reset & Load" | restart without stored code | restart **without** stored code; the stored script is killed |
| 2 | "Reload Flash & Load" | restart **with** stored code | restart with stored code, then stream on top |

Other stream details [source]:
- No CRC and no contiguity check. The firmware only compares `offset` with the previous offset to detect a duplicate,
  and replays the last result without writing (fw/lispif.c:519-523).
- The restart for offset 0 runs **before** the duplicate check. A repeated offset-0 packet restarts LispBM again
  (fw/lispif.c:504-512 come before 519). [source] Retrying offset 0 is not idempotent. [inference]
- Completion is detected when `offset + written == tot_len`. The writer is then closed and the const-heap index saved
  to flash (fw/lispif.c:614-621).

### 1.7 `COMM_LISP_GET_STATS` (134) and `COMM_LISP_REPL_CMD` (138)

Not needed for upload. Section 9 covers what they do to a running script.

---

## 2. Stored code format

### 2.1 Layout in flash sector 10 (`0x080C0000`, `CODE_IND_LISP`) [source]

Built by `lispPackImports` (vt/codeloader.cpp:173-339) and `lispUpload` (vt/codeloader.cpp:374-380). The firmware
parses it in `qmlui_check` (fw/flash_helper.c:472-490), `flash_helper_code_data` and `_size`
(fw/flash_helper.c:216-240), and `lispif_restart` (fw/lispif.c:704-819).

| Sector offset | Size | Field | Source |
|---|---|---|---|
| 0 | 4 | `uint32 N` = length of everything from offset 8 to the end (`vb.size() - 2`) | vt/codeloader.cpp:378; fw/flash_helper.c:234-236, 479 |
| 4 | 2 | `uint16 crc16` over offsets 6 .. 8+N-1 (flags + content, N+2 bytes) | vt/codeloader.cpp:376, 379; fw/flash_helper.c:480-484 |
| 6 | 2 | `uint16 flags`; VESC Tool always writes 0 | vt/codeloader.cpp:176; fw/flash_helper.c:242-252 |
| 8 | C+1 | script text, NUL-terminated (a NUL is appended if missing) | vt/codeloader.cpp:177-185 |
| 8+C+1 | 2 | `int16 num_imports` (0 if none) | vt/codeloader.cpp:314 |
| ... | per import | `name\0`, `int32 offset`, `int32 len` | vt/codeloader.cpp:318-328; vt/vbytearray.cpp:163-167 |
| ... | | NUL padding, so each blob starts 4-byte aligned relative to sector byte 8 | vt/codeloader.cpp:319-322, 330-333 |
| ... | len | import blob: the raw file plus one appended NUL; `len` includes that NUL | vt/codeloader.cpp:290-291, 326 |

- Import `offset` is relative to sector byte 8, the firmware's `code_data` (vt/codeloader.cpp:316 subtracts the 2 flag
  bytes; fw/lispif.c:811 adds it to `code_data`). [source]
- Alignment [source]: blobs are 4-byte aligned relative to byte 8, so their absolute addresses are 4-byte aligned
  (`0x080C0008 + 4k`). The comment says it is "in case this is loaded as code" (vt/codeloader.cpp:319).
- Terminators [source]: the script ends with NUL; each name ends with NUL; each blob ends with an extra NUL counted in
  `len`. There is no table terminator: the count is in `num_imports`.
- `reduceLisp` (off by default) strips comments, indentation and the `(import ...)` lines from the text
  (vt/codeloader.cpp:113-171; vt/pagelisp.cpp:667). [source]
- Import paths [source]: the path inside `(import "path" 'tag)` is resolved against the editor's folder, then as given.
  The line parser requires `(import ` at the start of the line and takes the tag from after the last `"`
  (vt/codeloader.cpp:1393-1428, 246-249). `pkg::` and `pkg@` imports pull from `.vescpkg` files
  (vt/codeloader.cpp:224-283).

Worked example [inference, from file sizes]: `engine_foc_generator.lisp` (53,217 B, import at line 128) plus
`hall_capture.bin` (2,528 B) packs to about 55.8 kB, about 146 write chunks of 384 B. That is well under the limit.

### 2.2 How the firmware uses it

At boot and on every restart, `lispif_restart(print, load_code, load_imports)` (fw/lispif.c:694-853) does this
[source]:

1. Reads `code_data` and `code_len`. Both are 0 unless the CRC is good (fw/lispif.c:704-705;
   fw/flash_helper.c:216-240).
2. Proceeds only if `!load_code || (code_data && code_len > 0)` (fw/lispif.c:707). At boot `load_code` is true, so
   **an invalid or empty code area means LispBM is not started at all**.
3. If an earlier image write collided, erases sector 8 (fw/lispif.c:710-713).
4. Stops the old instance, including native libraries (fw/lispif.c:717, 680-681).
5. Sets `code_chars = strnlen(code_data, code_len)` (fw/lispif.c:725).
6. Opens the image in sector 8 (`0x08080000`), 128 kB (fw/lispif.c:732-747).
7. If no image exists, or its version string differs from the firmware's app CRC (`%08X`), erases sector 8, creates a
   new image, and **only then sets `load_imports`** (fw/lispif.c:749-762).
8. `lbm_image_boot()` restores symbols, constant bindings and extensions saved in the image (fw/lispif.c:764;
   fw/lbm_image.c:1136-1377).
9. If the restored global environment holds a closure `main`, runs `"(main)"` **instead of the stored text**
   (fw/lispif.c:776-786, 830).
10. Import table, only when `load_imports` is set (fw/lispif.c:798-819):
    - It is present if `code_len > code_chars + 3`.
    - Reading starts at `code_chars + 1`: `uint16 num_imports`, then the entries. Only `0 < n < 500` is accepted.
    - For each entry it reads `name`, `int32 offset` and `int32 len`, then calls `lbm_share_array_const(code_data +
      offset, len)` and `lbm_define(name, ...)`.
    - There is no bounds check of `offset` or `len` against `code_len`. Only the CRC protects the table.
    - It then calls `lbm_image_save_global_env()`.
11. Evaluates the text with `lbm_load_and_eval_program_incremental` on a string channel over `code_data` in flash, as
    context `"main-u"` (fw/lispif.c:821-840).

Imports [source]:
- `lbm_share_array_const` writes an array header into the constant heap, the bottom of sector 8. The header's data
  pointer points at the blob in sector 10 (fw/lbm_c_interop.c:221-249).
- The import blob is never copied. It stays in sector 10.
- `lbm_image_save_global_env` saves a constant value as `BINDING_CONST` (fw/lbm_image.c:974-977). On later boots,
  where the image exists and the version matches, `load_imports` stays false and the import names come back from the
  image, not from the table (fw/lispif.c:752-762; fw/lbm_image.c:1176-1197). [source + inference]

`(import ...)` evaluated at runtime is a no-op: `"import"` is bound to `ext_empty`, which returns `t`
(fw/lispif_vesc_extensions.c:6527, 5767-5770). Only the stored table defines import names. [source]

`(load-native-lib name)` (fw/lispif_c_lib.c:738-1117) [source]:
- Accepts any array (`lbm_is_array_r`, line 741).
- Takes `addr = array->data` (line 1084): the blob's flash address in sector 10.
- Adds 4 to skip the "program pointer", sets the Thumb bit, and **calls that address directly** as
  `bool init(lib_info*)` (lines 1097-1099).
- Does no magic-number, CRC, size or address-range check before jumping.
- At most `LIB_NUM_MAX` = 10 libraries can be loaded (line 64).
- A library's `stop_fun` must pass `utils_is_func_valid` (lines 1101-1105).
- The library code **executes in place from sector 10**. [inference from 1084-1099 and lbm_c_interop.c:221-249]

---

## 3. What is erased and written

### 3.1 STM32F4 sector map in `flash_helper.c` [source]

Defines are at fw/flash_helper.c:41-65 and 99-100.

| Sector | Address | Size | Use | Source |
|---|---|---|---|---|
| 0 | 0x08000000 | 16 kB | vector table (app) | :67-68 |
| 1-2 | 0x08004000 | 2×16 kB | EEPROM emulation (config) | :69, 328 |
| 3-7 | 0x0800C000 | 16+64+3×128 kB | app firmware | :71-72 |
| 8 | 0x08080000 | 128 kB | `LISP_CONST_BASE`: LispBM image and constant heap; also `NEW_APP_BASE` (firmware-update staging) | :44, 49 |
| 9 | 0x080A0000 | 128 kB | `QMLUI_BASE`; also new-app staging | :45, 47 |
| 10 | 0x080C0000 | 128 kB | `LISP_BASE`: stored script and imports; also new-app staging | :45, 48 |
| 11 | 0x080E0000 | 128 kB | `BOOTLOADER_BASE` | :42, 277 |

`code_sectors[3] = {QMLUI_BASE, LISP_BASE, LISP_CONST_BASE}`, indexed by `CODE_IND_QML=0`, `CODE_IND_LISP=1`,
`CODE_IND_LISP_CONST=2` (fw/flash_helper.c:100; fw/flash_helper.h:25-27).

### 3.2 Erase path

`flash_helper_erase_code` (fw/flash_helper.c:184-208) [source]:

```c
	if (ind == CODE_IND_LISP || ind == CODE_IND_LISP_CONST) {
		lispif_stop();
	}
	uint8_t *ptr = flash_helper_code_data_raw(ind);
	bool has_data = false;
	for (int i = 0;i < (1024 * 128); i++) {
		if (*ptr != 0xFF) {        // ptr is never incremented
			has_data = true;
			break;
		}
	}
	if (!has_data) {
		return FLASH_COMPLETE;
	}
	...
	return erase_sector(flash_sector[code_sectors[ind]]);
```

- The "has data" scan **only tests the first byte** of the sector, because `ptr` never advances. If byte 0 is `0xFF`,
  the erase is skipped and success is reported. [source]
- Sector 10's byte 0 is the high byte of N, which is `0x00` after any upload, so in practice sector 10 is erased.
  [inference]
- Sector 8's byte 0 is constant-heap word 0. Symbol names created at runtime (fw/symrepr.c:390-411, 428-439) and import
  array headers (fw/lbm_c_interop.c:221-249) go there, so any script that defines a symbol makes it non-`0xFF`.
  [source + inference]
- If byte 0 were still `0xFF`, sector 8 would not be erased. The old image would survive, and with a matching version
  the next restart would not read the new import table (fw/lispif.c:752-762). [inference, edge case]

`erase_sector` (fw/flash_helper.c:414-436) [source]:

```c
	mc_interface_ignore_input_both(5000);
	mc_interface_release_motor_override_both();
	if (!mc_interface_wait_for_motor_release_both(3.0)) {
		return 100;
	}
	utils_sys_lock_cnt();
	timeout_configure_IWDT_slowest();
	uint16_t res = FLASH_EraseSector(sector, ERASE_VOLTAGE_RANGE);
	FLASH_Lock();
	timeout_configure_IWDT();
	mc_interface_ignore_input_both(100);
	utils_sys_unlock_cnt();
```

- It always releases the motor and makes it ignore input for 5 s, before it knows whether the erase will happen.
- It waits up to 3 s for the motor state to become OFF (fw/mc_interface.c:964-989), and returns 100 (failure) if it
  doesn't. On that path the flash is left unlocked.
- `utils_sys_lock_cnt` calls `chSysLock()` (fw/util/utils_sys.c:39-44), so the RTOS is locked for the whole erase.
- The watchdog is switched to prescaler 256, reload 4000 for the erase (fw/timeout.c:128-153).
  [inference, arithmetic] That is 256 × 4001 / LSI. Using the LSI range in the comment at fw/timeout.c:57-58 (17 to
  47 kHz), the window is about 22 to 60 s.
- [inference] While the RTOS is locked, the USB thread can't run, so the board is silent until the erase ends.

### 3.3 Write path

`write_data` (fw/flash_helper.c:438-470) [source]:
- Releases the motor and waits for it in the same way as `erase_sector`, **for every packet**.
- Locks the RTOS, then calls `FLASH_ProgramByte(base + i, data[i])` for `i < len`.
- There is no range check.

The `COMM_LISP_WRITE_CODE` handler [source]:

```c
		uint32_t qmlui_offset = buffer_get_uint32(data, &ind);   // ind = 4
		...
		uint16_t flash_res = flash_helper_write_code(... CODE_IND_LISP, qmlui_offset, data + ind, len - ind);
```

`flash_helper_write_code` returns `write_data(flash_addr[code_sectors[ind]] + offset, data, len)`
(fw/flash_helper.c:210-214). The handler is at fw/commands.c:1456-1463.

### 3.4 Can these commands reach anything outside the Lisp area?

| Command / path | Firmware image (0,3-7) | Bootloader (11) | Config (1-2) | Notes |
|---|---|---|---|---|
| `LISP_ERASE_CODE` | no | no | no | erases only sectors 8 and 10 via `code_sectors[]` (fw/commands.c:1441,1445; fw/flash_helper.c:100,207) [source] |
| `LISP_WRITE_CODE` with `offset + len <= 0x20000` | no | no | no | stays inside sector 10 [source] |
| `LISP_WRITE_CODE` with `offset + len > 0x20000` | **yes, if the offset wraps** | **yes** | **yes, if the offset wraps** | `0x080C0000 + offset` is unchecked 32-bit arithmetic. Offsets from 0x20000 hit sector 11; very large offsets wrap to `0x08000000+` (fw/flash_helper.c:213) [source]. Programming can only clear bits, which corrupts the target. [inference] |
| `LISP_WRITE_CODE` with a payload of 1 to 3 bytes after the ID | **yes** | **yes** | possible | `len - ind` is unsigned with `len < 4`, so it underflows to about 4 G bytes. That programs from sector 10 through the bootloader to the end of flash (fw/commands.c:1463; fw/flash_helper.c:453) [source + inference] |
| `LISP_READ_CODE` | no writes | no | no | a negative `len` reaches `memcpy` (fw/commands.c:1417,1426); likely a crash [inference] |
| `LISP_STREAM_CODE`, `LISP_REPL_CMD`, `SET_RUNNING` | no | no | no | do write **sector 8** at runtime: symbol names and const data (fw/lispif.c:876-902; fw/symrepr.c:390-439). Sector 8 is erased on restart if the image version mismatches or a write collided (fw/lispif.c:710-713,755-757) [source] |
| Script / native library at runtime | possible | **yes** | **yes** | native libraries get `read_nvm`, `write_nvm` and `wipe_nvm` (fw/lispif_c_lib.c:1023-1026). These address **sector 11** (`ADDR_FLASH_SECTOR_11 + address`), and `wipe_nvm` erases sector 11, which `flash_helper.c` defines as the bootloader (fw/flash_helper.c:42,492-534,277). Libraries also get `store_cfg` and `store_eeprom_var` (vesc_c_if.h:535,560). Lisp has `cmds-proc` (fw/lispif_vesc_extensions.c:6780) and `image-save` (6776) [source] |

Related, not on the Lisp path [source]: a firmware upload (`COMM_ERASE_NEW_APP`, already blocked) erases sectors 8, 9
and 10 depending on image size, which wipes the image, QML and the stored script (fw/flash_helper.c:132-174).

The app CRC check covers only sector 0 and sectors 3-7 (fw/flash_helper.c:317-336, 377-408). Corruption in sectors 8
to 11 is not detected by `flash_integrity_check_thread` (fw/main.c:101-114). [source]

---

## 4. VESC Tool upload sequence (GUI "Upload" button)

From `PageLisp::on_uploadButton_clicked` (vt/pagelisp.cpp:633-700) [source]:

1. **Pack.** `lispPackImports(codeStr, editorPath, reduceLisp)`; an empty result means a missing import, and the
   upload stops (vt/pagelisp.cpp:666-670).
2. **Erase.** `lispErase(vb.size() + 100)` sends `COMM_LISP_ERASE_CODE` once and waits up to **8 s** for the reply.
   There is **no retry**. Timeout, or `ok = 0`, aborts (vt/pagelisp.cpp:683; vt/codeloader.cpp:63-111).
3. **Write.** `lispUpload(vb)` (vt/codeloader.cpp:374-457):
   - Prepends `[uint32 N][uint16 crc]`.
   - Refuses if the total is over `128*1024 - 6` on `HW_TYPE_VESC`.
   - Sends 384-byte chunks at offsets 0, 384, 768, ... in order.
   - Waits **1 s** per reply, up to **5 tries** per chunk. A reply with `ok = 0` is retried too (`res >= 0` test,
     lines 416-428).
   - **The offset in the reply is ignored** (lines 403-405), so a late reply to an earlier try can be taken as the
     answer to a later one.
   - The Cancel button sets `mAbortDownloadUpload`, which stops between chunks and leaves the area half-written
     (lines 434, 450-452).
4. **No read-back or verify step** exists in VESC Tool's upload.
5. **Run.** If "AutoRun" is checked (it is by default, vt/pagelisp.ui:336-344), it sends `COMM_LISP_SET_RUNNING(1)`
   and doesn't wait for the reply (vt/pagelisp.cpp:623-626, 697-699).

Other entry points use the same order [source]:
- `lispUploadFromPath` (vt/codeloader.cpp:470-487) erases and uploads, with no run step.
- `installVescPackage` (vt/codeloader.cpp:997-1012) erases, uploads, then runs. With no Lisp data it does an erase only,
  with size 16.

**Erase duration** is not in either source. The only figures are VESC Tool's 8 s timeout and the firmware's 3 s motor
wait per sector, before each of the two sector erases. [source] Worst case before the reply [inference]: about 2 × 3 s
of motor wait plus two 128 kB sector erases.

---

## 5. Streaming to RAM (`COMM_LISP_STREAM_CODE`)

What it does [source]:
- It feeds the raw script text into a buffered character channel. The parser evaluates it as context `"main-s"`
  while the bytes arrive (fw/lispif.c:569-583, 596-612).
- VESC Tool streams the editor text as UTF-8 **without `lispPackImports`**: no header, no import table
  (vt/pagelisp.cpp:783-804; vt/codeloader.h:46-48; vt/codeloader.cpp:489-537). It uses 384-byte chunks and a 4 s
  reply timeout, with no retry; any non-zero result aborts (vt/codeloader.cpp:500-530).

Imports and native libraries [source + inference]:
- In streamed code, `(import ...)` is a no-op returning `t` (fw/lispif_vesc_extensions.c:6527, 5767-5770). Import
  names are only defined from the **stored** table, at a restart that creates a new image (fw/lispif.c:752-762,
  798-819), or restored from the image on later restarts (fw/lbm_image.c:1176-1197).
- So streaming **cannot bring in a new native library**. A streamed script could only reach a library already
  uploaded to sector 10 by a previous store, through a name restored from the image.
- `load-native-lib` itself accepts any array (fw/lispif_c_lib.c:741-745). Whether a library could be run from a RAM
  array is not settled (open question 3).

Effect on the stored and running scripts [source]:
- The stored script in sector 10 is never touched. The stream handler has no `flash_helper_write_code` or
  `erase_code` call (fw/lispif.c:492-640).
- The running script depends on the mode table in 1.6. Mode 1, VESC Tool's default, kills the stored script.
  Mode 2 restarts it. Mode 0 leaves it running and adds the streamed code beside it.

Flash is still written during a stream [source]:
- New symbol names and any `@const-start` or `move-to-flash` data go into the sector-8 constant heap
  (fw/symrepr.c:390-439; fw/lispif.c:876-902).
- The const-heap index is saved when the stream ends (fw/lispif.c:617).
- Streaming is therefore not RAM-only. [inference] It consumes sector-8 space until the next sector-8 erase.

After a power-cycle [source + inference]:
- The stored script runs again (fw/lispif.c:90-96 → 694-853). The streamed code is gone, except for its symbols and
  const data left in sector 8.
- **Exception:** if the streamed code called `(image-save)` with a `main` function defined, the image now holds that
  `main`. The next boot runs `"(main)"` from the image instead of the stored text (fw/lispif_vesc_extensions.c:6232-6253;
  fw/lispif.c:776-786, 830).

---

## 6. Failure behaviour

### 6.1 Power lost mid-erase or mid-write

- **Mid-write, or after the erase of sector 10:** the CRC over N bytes fails, so `code_data`/`code_size` are 0
  (fw/flash_helper.c:472-490, 216-240). The boot-time `lispif_restart(false, true, true)` then does nothing, and
  **Lisp is not started** (fw/lispif.c:94-95, 707). [source] The rest of the firmware, including USB, still boots;
  `main()` doesn't depend on Lisp (fw/main.c:293-420). [source]
- **N itself corrupt or `0xFFFFFFFF`:** `N > 128K-8`, so the check fails safe (fw/flash_helper.c:482-487). [source]
- **Mid-erase of sector 8:** `ERASE_CODE` erases 8 first, then 10 (fw/commands.c:1441, 1445), so sector 10 can still
  hold the old valid code.
  - On boot the image is accepted if its **last word** equals `0xBEEF2001` and the version string matches
    (fw/lbm_image.c:1087-1090, 446-460; fw/lispif.c:755). [source]
  - If that word was erased, a fresh image is made (fw/lispif.c:755-762). [source]
  - The image has **no CRC**. If the magic and version survive but other words were damaged, `lbm_image_boot` would
    parse damaged entries (fw/lbm_image.c:1136-1377). [inference] Whether a partly erased sector can look like that is
    an ST hardware question (open question 1).
- Erase and write are unrecoverable mid-operation. There is no staging copy or transaction: the old code is gone once
  sector 10 is erased. [source: no such mechanism in fw/flash_helper.c]

### 6.2 Corrupt or partly written code area at the next boot

Covered by the CRC, see 6.1: Lisp is not started and the board otherwise runs. `COMM_LISP_READ_CODE` then replies
`[130][0][0]` (fw/commands.c:1407-1415). `SET_RUNNING(1)` replies 0 (fw/lispif.c:707, 844). [source]

### 6.3 A script or native library that crashes or hangs at boot

- **Lisp start time** [source]: `lispif_init` runs from the `comm_block` thread after `main_init_done()`, so after USB,
  the watchdog and the 500 ms main sleep (fw/commands.c:2061-2075; fw/main.c:342-404).
- **Watchdog-reset rule** [source]: Lisp is skipped if `timeout_had_IWDG_reset()` or if the first logged fault is
  `FAULT_CODE_BOOTING_FROM_WATCHDOG_RESET` (fw/lispif.c:90-96).
  - That fault is logged by motor init, which also clears the reset flag: FOC in mcpwm_foc.c:579-584, BLDC in
    fw/mcpwm.c:444-445.
  - The comment at mcpwm_foc.c:579-581 notes that this "also will trigger after running the bootloader". The first
    boot after a firmware update therefore skips Lisp too.
- **What the watchdog watches** [source]: it is fed only while the motor-control thread, and CAN if enabled, report
  iterations (fw/timeout.c:246-270). **The Lisp thread is not monitored.** It runs at `NORMALPRIO - 1`
  (fw/lispif.c:768) and the timeout thread at `NORMALPRIO` (fw/timeout.c:83).
  - [inference] A Lisp infinite loop, or a native call stuck in the eval thread, won't cause a watchdog reset. The
    board stays up with Lisp stuck.
  - [inference] Only something that starves or locks the higher-priority threads triggers the watchdog, for example a
    library thread raised with `thread_set_priority` or interrupts left locked.
- **Hard faults do not count as watchdog resets** [source]: `HardFault`, `MemManage`, `BusFault` and `UsageFault` call
  `main_stop_motor_and_reset()`, which ends in `NVIC_SystemReset()` (fw/irq_handlers.c:81-95; fw/main.c:425-464).
  - [inference] A native library that faults during `load-native-lib` at boot causes a software reset. Lisp starts
    again and faults again: a **boot loop**.
  - **The SAFETY.md recovery note ("the firmware doesn't start Lisp after a watchdog reset… USB should stay usable")
    does not cover this case.**
  - Each loop iteration still runs the full `main()`, including USB, before Lisp starts. Whether the host can get an
    `ERASE_CODE` through in that window is not settled (open question 4).
- **Deliberate watchdog crash** [source]: when the firmware stops native libraries (erase, restart, `SET_RUNNING(1)`,
  stream mode 1 or 2, `:reset`), it asks each library thread to terminate. A thread still running after 2 s makes the
  firmware lock the system on purpose, forcing a watchdog reset "to not start lbm after reboot"
  (fw/lispif_c_lib.c:129-144, 1147-1162).
- **Possible endless wait** [source]: `lispif_stop` loops `lbm_kill_eval()` until the eval thread exits, **with no
  timeout** (fw/lispif.c:685-689). [inference] If the eval thread is stuck in a native call, the command thread that
  sent `ERASE_CODE` or `SET_RUNNING(1)` waits forever. No watchdog reset follows, since the monitored threads still
  run, and the board stops answering USB until power-cycled.
- **Manual wipe** [source]: holding HALL2 tied to SWCLK at power-up erases sectors 8, 10 and 9 before USB starts
  (fw/main.c:262-293, 332-339).

### 6.4 The 0.5 s REPL guard and similar guards

- **REPL** [source]: `if (UTILS_AGE_S(repl_time) <= 0.5) return;` drops any REPL command within 0.5 s of the last REPL
  evaluation, **with no reply** (fw/lispif.c:285-287). `repl_time` is set when an expression is started with a reply
  function (fw/lispif.c:474-476). Results are printed only if the evaluation finishes within 0.5 s
  (fw/lispif.c:655-663).
- **Stream** [source]: duplicate-offset replay (fw/lispif.c:519-523); 1500 ms reader-close wait (537-554); 1000 ms
  pause (559); 1500 ms per-byte channel timeout (595-611).
- **Erase and write** [source]: the motor-release wait of up to 3 s (fw/flash_helper.c:422-424, 446-448). Erase and
  write have **no** time-based guard and no busy flag, and nothing rejects a write that arrives during or before an
  erase.
- **`GET_STATS`** [source]: no reply at all when Lisp isn't running (fw/lispif.c:212-219). VESC Tool throttles it on
  the host side (vt/commands.cpp:2244-2249).
- **Blocking-thread commands** (terminal, detection) [source]: dropped silently while another one runs
  (fw/commands.c:1684-1716). Not relevant to Lisp upload.

### 6.5 A running script while its code is erased or rewritten

- **Erase** [source]: stops native libraries and kills the evaluator before erasing (fw/commands.c:1440;
  fw/flash_helper.c:186-188; fw/lispif.c:676-692). It does this **even if the erase then fails or is skipped**.
  Lisp stays stopped until `SET_RUNNING(1)`, a stream, a REPL command or a reboot. The script owns throttle and kill,
  so engine control stops at this point. [inference for the engine consequence]
- **Write** [source]: does **not** stop Lisp (fw/commands.c:1454-1473). [inference] In VESC Tool's order nothing runs
  during the writes because the erase stopped it. If a script were running, the text it reads incrementally and any
  native code would both be in sector 10, being overwritten.
- **Restarting Lisp during a write** [source]:
  - A REPL command restarts LispBM without stored code if it isn't running (fw/lispif.c:289-291).
  - A stream at offset 0 restarts it (fw/lispif.c:504-512).
  - `SET_RUNNING(1)` replies 0 mid-upload, because the CRC isn't valid yet (fw/flash_helper.c:211-212, 472-490).
- **Motor** [source]: every erase and every write chunk releases the motor and makes it ignore input for 5 s
  (fw/flash_helper.c:419-420, 443-444).

---

## 7. Reading the stored code back (backup)

Possible [source]: `COMM_LISP_READ_CODE` returns exactly the N stored bytes from sector byte 8: script text, NUL,
import table, padding and import blobs (fw/commands.c:1402-1429). Section 1.4 has the layout.

- VESC Tool reads it back and splits it with `lispUnpackImports` (vt/codeloader.cpp:341-372, 539-677). [source]
  - That function would also strip a leading `00 00` flags pair when given package data (vt/codeloader.cpp:346-348).
  - It subtracts 1 from each import length to drop the padding NUL (line 362).
- Not readable [source]: the 8-byte header (N, CRC, flags) and sector 8 (image and constant heap). No command reads
  sector 8.
- A non-empty reply proves the firmware's CRC check passed (fw/flash_helper.c:216-224). [source] The tool can also
  compute its own CRC over `00 00 + data`, since VESC Tool's flags are always 0. [source + inference]
- Restoring a backup [inference]: write `[uint32 N][crc16(00 00 + data)][00 00][data]` through the normal
  erase-and-write path. Import offsets are relative to sector byte 8 and the sector base is fixed, so a byte-for-byte
  restore puts native code at the same absolute addresses.
- What a backup doesn't capture [inference]: a script that depended on `image-save` state or on a constant heap built
  by earlier streams. After a restore the image is rebuilt from the code.

---

## 8. Size limits

| Limit | Value | Source |
|---|---|---|
| Firmware: stored N (bytes from offset 8) | ≤ 131,064 (`128*1024 - 8`); larger means invalid | fw/flash_helper.c:50, 482 (`LISP_MAX_SIZE` at :51 is unused; `QMLUI_MAX_SIZE` is used for all areas) |
| Code sector | 131,072 B (one 128 kB sector) | fw/flash_helper.c:48, 64 |
| VESC Tool: header + data | ≤ `128*1024 - 6` = 131,066 B, so N ≤ 131,058 | vt/codeloader.cpp:382-393 |
| Packet payload | 512 B unless the hardware overrides it | fw/packet.h:27-28 |
| Write chunk | VESC Tool 384 B; firmware has no check (keep `4 + data ≤ 512`) | vt/codeloader.cpp:435 |
| Read chunk | firmware ≤ `PACKET_MAX_PL_LEN - 10` = 502 B; VESC Tool 400 B | fw/commands.c:1417; vt/codeloader.cpp:575 |
| Stream chunk | VESC Tool 384 B; no total limit in the firmware | vt/codeloader.cpp:521 |
| Imports | `0 < num_imports < 500` | fw/lispif.c:803; vt/codeloader.cpp:358 |
| Native libraries loaded at once | 10 | fw/lispif_c_lib.c:64 |
| Image and constant heap (sector 8) | 128 kB shared: constant heap grows up, image entries grow down | fw/lispif.c:734; fw/heap.c:1298-1299 |

---

## 9. Other safety-relevant facts

Commands that resume or restart a script [source]:
- `GET_STATS` always calls `lbm_continue_eval()` (fw/lispif.c:278).
- `SET_RUNNING(1)` is a full restart (fw/lispif.c:194).
- A REPL command starts LispBM without stored code if it isn't running (fw/lispif.c:289-291).
- A REPL expression pauses and then continues the evaluator (fw/lispif.c:464-472).
- `STREAM_CODE` at offset 0 can start or restart LispBM (fw/lispif.c:504-512).
- The `:reset` and `:continue` meta commands (fw/lispif.c:432-442).
- Restarts stop native libraries first (fw/lispif.c:717, 680-681; fw/lispif_vesc_extensions.c:6451).

Other facts:
1. **The bootloader sector is open to native libraries.** `wipe_nvm` erases sector 11 and `write_nvm` writes it
   (fw/flash_helper.c:492-534; fw/lispif_c_lib.c:1023-1026). [source] A library binary can't be checked for this from
   Lisp; only its C source or a disassembly could show it. [inference]
2. **`WRITE_CODE` has no range or length check** (section 3.3/3.4). [source] The host must enforce
   `0 ≤ offset`, `offset + len ≤ 0x20000` (better `≤ N + 8`) and `len ≥ 1` before the packet leaves.
3. **`READ_CODE` has a signed range check** (section 1.4). [source]
4. **Hard faults reboot through a software reset, not the watchdog**, so Lisp starts again and a crashing library can
   boot-loop (section 6.3). [source + inference]
5. **`COMM_REBOOT` is `NVIC_SystemReset()`** (fw/commands.c:695-698), so Lisp starts after it.
   **`COMM_SHUTDOWN` with restart = 1 locks up until the watchdog fires** (fw/commands.c:1621-1638), so Lisp is
   skipped after it. [source + inference from 6.3]
6. **Pausing doesn't stop native libraries** (section 1.5). [source + inference]
7. **An image `main` overrides the stored text** (section 2.2 step 9), so `(image-save)` in any script, streamed or
   stored, changes what boots. [source]
8. **Native code runs from sector 10** and has no integrity check beyond the stored CRC (section 2.2). [source]
9. **The firmware upload path erases the Lisp sectors too** (section 3.4). [source]
10. **Sector 8 is written at runtime** by streams, the REPL and scripts. It has no CRC and is only checked by a magic
    word and version (fw/lbm_image.c:1087-1090). [source]

---

## 10. Inferred implications for the tool

These are inferences from the facts above, not firmware behaviour.

- Add `COMM_LISP_READ_CODE`, `COMM_LISP_ERASE_CODE` and `COMM_LISP_WRITE_CODE` to the allow-list only with structural
  checks:
  - Write: payload ≥ 5 bytes; `offset` and `offset + len` inside `[0, N + 8]`, with `N + 8 ≤ 131,072`.
  - Read: `len` in `1..400`, `offset ≥ 0`.
  - Erase: exactly the ID plus 4 bytes, as VESC Tool sends it.
- Match each write reply's echoed offset before sending the next chunk, unlike VESC Tool.
- Never retry a stream chunk at offset 0.
- Order:
  1. Back up with `READ_CODE`.
  2. Check that the motor is idle.
  3. Erase once, with a timeout of at least 8 s.
  4. Write each chunk once, offset-matched.
  5. Read back every byte, which also proves the firmware CRC.
  6. Only then send `SET_RUNNING(1)`.
- The rule in SAFETY.md, "Run new code from RAM first (`COMM_LISP_STREAM_CODE`)", **cannot apply to native
  libraries**: streaming can't import a new library (section 5). A first boot of a new library is always from flash.
- Refuse to upload or stream code that calls `image-save`, `cmds-proc` or the `*-nvm` interfaces, and libraries whose
  source uses `write_nvm`, `wipe_nvm` or `store_cfg`.

---

## 11. Open questions not settled from source

1. **Erase time and interrupted-erase state.** How long a 128 kB sector erase takes at the board's
   `ERASE_VOLTAGE_RANGE` (fw/flash_helper.c:78), and what a power loss mid-erase leaves in the sector. Neither is in
   the firmware or VESC Tool. This needs the STM32F4 datasheet and reference manual, or a bench measurement.
2. **Writing over non-erased bytes.** Whether reprogramming a byte with the same value is harmless on STM32F4. VESC
   Tool's 5-try write retry relies on it (vt/codeloader.cpp:416-428). The firmware doesn't say.
3. **Native libraries from RAM.** Whether a `load-native-lib` on a RAM array, for example a byte-array literal in
   streamed code, would work. It depends on how the `.bin` is linked: position independence, and what the "program
   pointer" at offset 0 holds (fw/lispif_c_lib.c:1097). The library build files weren't checked.
4. **USB window in a boot loop.** Whether, in a hard-fault boot loop, USB is up long enough before Lisp starts for a
   host to send `COMM_LISP_ERASE_CODE`. The timing of `main()` versus `lispif_init` wasn't measured.
5. **Which commit the board runs.** The board's firmware commit is unknown. `release_7_00` moved as late as
   2026-11-05, and `lispif.c` changed on the branch in 2026. Compare `fw_info`'s "Git Hash"
   (fw/terminal.c:1150-1153; `fw_info` is already allowed) with `e877eae` before relying on line-level behaviour.
6. **Erase timing at the protocol level.** Whether the comm thread's `chSysLock` during erase drops or only delays
   USB packets that arrive meanwhile. The USB driver wasn't read.
7. **Hardware overrides.** Whether the Flipsky hardware config overrides `PACKET_MAX_PL_LEN`, or the sector map. The
   hardware headers weren't fetched.
8. **What `hall_capture.bin` uses.** Whether it calls `write_nvm`, `wipe_nvm`, `store_cfg` or creates
   high-priority threads. Its source (`native/hall_capture/code.c`) wasn't audited here; a quick grep found no
   `nvm`, `flash`, `store_cfg` or `eeprom`.
9. **Bootloader write protection.** Whether the bootloader sector is write-protected by option bytes on these boards.
   If so, the sector-11 risks in 3.4 and 9.1 would fail with `WRPERR` instead of corrupting. Nothing in the source
   sets or checks option bytes.
