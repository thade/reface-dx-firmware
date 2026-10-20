# Yamaha reface DX firmware notes

Goal: add a way to stop the reface DX transmitting MIDI clock (0xF8), which Yamaha
confirms cannot be switched off in stock firmware. Secondary question: could the
reface CP firmware run on DX hardware. Everything in "Verified" below was checked
against the actual update file on 2026-10-19; "Reported" items come from
teardowns and forums and have not been checked on hardware.

## Summary

- MIDI clock disable: feasible as a firmware patch. The update image is plain,
  unencrypted big-endian ARM/Thumb code with readable strings. Main open question
  is whether the bootloader verifies an image-level checksum (see "Unknowns").
- CP firmware on a DX: not viable. CP/YC and CS/DX are different boards on
  different Yamaha chips (SWX08 vs SSP2), and the CP needs a 64 MB sample ROM
  the DX does not have.
- Non-firmware workaround: a DIN MIDI filter dropping 0xF8 and 0xFE. No clean
  equivalent exists for USB MIDI, which is why the firmware route matters.

## Layout of this repo

```
reface_csdx_fw/
  UpdaterGuide_en.pdf              Yamaha's official update procedure
  tools/refacefw.py                extract / repack / roundtrip the updater SMF (byte-identical)
  tools/find_realtime.py           locate MIDI real-time senders in the image (capstone, BE Thumb)
  tools/patch_clock.py             apply the no-clock patch and build a patched updater SMF
  tools/patch_clock_menu.py        add a FUNCTION "MIDI 2" page with a clock on/off switch
  tools/selftest.py                rebuild all three payloads in memory, check pinned SHA-256 (stdlib only)
  tools/verify_menu.py             emulate the MIDI 2 page code under unicorn (draws, trampoline, keys, page apply)
  out/reface_updater_CSDX_v130.mid the untouched payload from the official updater
  out/reface_csdx_main_v130.bin    decoded raw image (1,966,080 bytes)
  out/*_noclock.*                  patched image and patched updater payload
  out/*_clockmenu.*                MIDI 2 page image and updater payload
  tools/make_updaters.sh           build re-signed stock / no-clock / clock-menu updater apps
  updater/                         (git-ignored) the zip, orig/, stock/, noclock/, clockmenu/ app copies
references/                        Yamaha manuals: Data List, Owner's, Reference, v1.30 Supplementary
```

Hardware test procedure: see [CHECKLIST.md](CHECKLIST.md). Narrative summary of findings: [WRITEUP.md](WRITEUP.md). FUNCTION menu / parameter system / LCD reverse-engineering notes: [MENU_RE.md](MENU_RE.md).

Python deps: the extract/patch/selftest tools need only the standard library.
`find_realtime.py` needs capstone and `verify_menu.py` needs unicorn (and uses
capstone when present): `cd reface_csdx_fw && python3 -m venv venv &&
venv/bin/pip install capstone unicorn`.

`out/` and `updater/` are git-ignored: the files are Yamaha's copyrighted firmware and are
reproducible from the official download. Regenerate with:

```bash
python3 tools/refacefw.py roundtrip "reface updater.dat"      # must print ROUNDTRIP OK
python3 tools/refacefw.py extract "reface updater.dat" out/reface_csdx_main_v130.bin
python3 tools/patch_clock.py out/reface_csdx_main_v130.bin out/reface_csdx_main_v130_noclock.bin out/reface_updater_CSDX_v130_noclock.mid
python3 tools/patch_clock_menu.py out/reface_csdx_main_v130.bin out/reface_csdx_main_v130_clockmenu.bin out/reface_updater_CSDX_v130_clockmenu.mid
python3 tools/selftest.py                                       # all three payload hashes must print OK
venv/bin/python tools/verify_menu.py                            # emulated behaviour checks, must print ALL OK
```

`tools/make_updaters.sh` runs both checks before building the apps.

## Verified: the update file

Source: `reface_updater_CSDX_v130-3_mac.zip` from Yamaha USA
(SHA-256 `291e5836af25d39e536ec2fd3d8cf82905edfce6d0d86692023656d9697889c2`).
The app bundle `reface updater.app` contains one payload,
`Contents/Resources/reface updater.dat` (2,250,052 bytes, SHA-256
`645309bef54e9444ebdeeacaa42f727b0fd0bdcfc06992ca1adde30a16112abb`).

- The payload is a Standard MIDI File, format 0, one track, 480 ppqn. The
  updater is just a SysEx player (its binary contains `SMF/MidiSysExEvent.cpp`,
  `SMFPlayerDoc`, `DTAMIDIManager_Mac.cpp`). It streams the file to the unit
  over USB MIDI while the unit is in Update mode.
- Update mode: power off, hold F#3 + A3 + C#4, power on. DX shows
  "reface DX Update" on the LCD. Keep the AC adaptor connected.
- 139 SysEx messages, all of the form

  ```
  F0 43 00 7F 1C <count hi> <count lo> 03 50 00 <data...> <checksum> F7
  ```

  `count` is 14-bit (7-bit pairs) and covers address + data. Checksum is the
  standard Yamaha bulk one: `(-(sum(address + data))) & 0x7F`. All 139 verify.
- Message 0 is a 9-byte header (`00 01 0A 00 00 10 08 00 00`, meaning not yet
  decoded). Messages 1..138 carry a 5-byte sub-header
  `01 <blk hi> <blk lo> <len hi> <len lo>` then packed data. Each block holds
  14322 raw bytes (2046 groups of 7 = 16368 packed); the length field is
  raw bytes + number of groups, i.e. packed bytes before padding to a whole
  group. SMF deltas: 0 before the header message, 1309 ticks before every
  block and before end-of-track. `refacefw.py roundtrip` rebuilds the
  original file byte for byte.
- 7-bit packing: groups of 8 bytes, first byte holds the MSBs, bit 6 belongs to
  the first of the following 7 data bytes, bit 0 to the last.
- Decoded image: exactly 1,966,080 bytes (0x1E0000); the last packed group
  carries 3 pad bytes. Everything from 0x140000 up is 0xFF erase fill, so the
  live program is about 1.25 MB in a 2 MB flash region.
- Not encrypted, not compressed. Code-region entropy is 6.3 to 7.0 bits/byte,
  strings and source paths are plaintext, no compression signatures found.
- Build stamp at the end of the image: `REFACE      MAIN` then
  `1302016MAR03144105`, i.e. v1.30 built 2016-03-03 14:41:05.
- Both `reface DX` and `reface CS` strings are present, plus `reface DX TstPrg`.
  It is one unified CS/DX binary that selects the model at runtime, consistent
  with the shared updater and shared main board.
- Eight non-text bytes follow the build stamp
  (`EE 1C 3B FD 42 FF 58 FF`). The first four are a **sampled checksum**
  computed by 0x0202D744: for each 512 KB chunk of the 2 MB region it adds the
  4 bytes at chunk+0 and at chunk+4, +8, +16, …, +0x40000 into four byte-wide
  lanes; `refacefw.sampled_lanes` reproduces `EE 1C 3B FD` exactly. A second
  routine (0x0202D858) checks the plain 8-bit sum of the region. Both are
  called by 0x0202D824, which in the main image is used by the factory test
  code; whether the bootloader runs the same checks is unknown. The no-clock
  patch leaves the sampled lanes untouched (0x2145E is not a sampled offset)
  but changes the byte-sum, and the unit accepted it, so the byte-sum is not
  enforced at boot. The menu patch keeps both equal to stock anyway: it starts
  its free-ROM data at file 0x140004 because 0x140000 (= 0x100000 + 0x40000)
  is sampled, and balances the byte-sum with a pad byte.

## Verified: the CPU and image layout

The firmware is **big-endian ARM**, not SH-2A as the sandsoftwaresound teardown
assumed for the SSP2. Evidence:

- Offset 0x00: eight `E5 9F F0 18` words = `LDR PC, [PC, #0x18]`, the classic
  ARM exception vector table, stored big-endian.
- Offset 0x20: vector targets `0x020204A8` (reset), `0x02020468`,
  `0x0202046C`, ... and `0x0101F000` for IRQ.
- Reset handler at 0x4A8 is ARM mode: `mov r6,r0; mov r7,r1; mov r8,r2;
  mov r0,#0xD2; msr cpsr_c,r0` then peripheral register setup.
- The bulk of the code is Thumb-1 read as big-endian halfwords. Example at
  file offset 0x730: `push {lr}; ldr r3,[pc,#0x18]; movs r2,#0x5a; movs r1,#0x20;
  str r2,[r3]; str r1,[r3,#4]`.
- Link/load base is **0x02020000** (RAM; a boot ROM copies the image there).
  With that base, 896 big-endian words point exactly at string starts and
  694 of 863 odd-valued pointers into the image land on Thumb function
  prologues. No other base comes close.
- Peripheral literal-pool addresses seen: `0x0700xxxx`, `0x0101xxxx`,
  `0x0401xxxx` (shared RAM). `0x0215xxxx` is inside this image (file
  0x13xxxx: the boot-copied screen block, see MENU_RE §1).

Ghidra setup: import `out/reface_csdx_main_v130.bin` as raw binary,
language `ARM:BE:32:v5t` (or v7 if v5t rejects anything), base address
`0x02020000`. capstone: `CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_BIG_ENDIAN`.

Rough code map (per 128 KB, counting Thumb push/pop/bl):

| file offset | content |
|---|---|
| 0x000000 - 0x05FFFF | dense Thumb code (thousands of BLs per 128 KB) |
| 0x060000 - 0x07FFFF | code, thinning out |
| 0x080000 - 0x09FFFF | data: FUNCTION menu strings at 0x80B0C, large zeroed tables |
| 0x0A0000 - 0x11FFFF | mixed tables and some code, high-entropy regions (likely DSP coefficient / wavetable data) |
| 0x120000 - 0x13FFFF | strings: model names at 0x12D84C/0x12D858, source paths at 0x1356B0 |
| 0x140000 - end      | 0xFF fill, build stamp + trailer at 0x1DFFD0 |

## Verified: useful landmarks in the image

Source file list compiled into the image (about 80 `.\src\*.c` paths). The
MIDI-relevant ones:

- `mdi_usbf.c`, `mdi_usbo.c` : USB MIDI function/out
- `sio_if.c` : serial interface (5-pin DIN side)
- `smd_receiver.c`, `rtg_if_message.c`, `rtg_an_an1x_midi.c` : MIDI receive / engine messaging
- `sys_scanmicom.c`, `scn_scanmicomdata.c` : link to the panel-scan Cortex-M3
- `lpr_rec.c`, `lpr_track.c`, `lpr_timer_func.c` : the Looper (tempo source)
- `sp2_cnfg.c`, `ssp_dspmodel.c`, `u5*_blk.c`, `up_*.c` : SSP2/DSP configuration
- `scn_model_fm.c` / `scn_model_an.c` : DX vs CS model-specific panel handling
- `tst*.c` : factory test program

FUNCTION menu text table at file offset 0x80B0C, 21-character strings on a
24-byte stride, e.g.

```
0x80B84  "  MIDI control off   "
0x80B9C  "  MIDI control ON    "
0x80BB4  "  Local control off  "
0x80C14  " MIDI transmit ch 1  "
0x80CA4  "MIDI control      off"
0x80CBC  "MIDI control       ON"
```

MIDI real-time constant sites in the code region (Thumb `movs rX,#imm`):
46 for 0xF8, 303 for 0xFE (most of the 0xFE ones are in one table-driven
block 0x4B900..0x4E900 and are probably unrelated), 53 for 0xFA, 114 for 0xFC.

## Verified: the MIDI clock sender

Found with `find_realtime.py`: the only function whose callers load each of
0xF8, 0xFA, 0xFB and 0xFC into r0 immediately before the call is
**0x02037C1C**, a 1-byte real-time transmit wrapper (it posts a type-9
message with the byte to the MIDI output queue via 0x02037A34). It has
exactly four callers:

| caller | byte | role |
|---|---|---|
| 0x02040400, 0x0204040C in fn 0x020403F4 | FB / FA | Start or Continue (chooses by 0x02041374 result) |
| fn 0x02040418 | FC | Stop |
| **0x0204145E**, fn 0x02041438 | **F8** | **Timing clock** |

The clock sender 0x02041438 (file 0x21438):

```
02041438  ldr  r3,[pc,#0x34]      ; &accumulator
0204143a  ldr  r3,[r3]
0204143c  push {r4,r5,r6,lr}
0204143e  adds r6,r0,r3           ; acc += ticks passed in r0
02041440  cmp  r6,#0x13
02041442  bls  02041466           ; < 20: just store
02041444  ...  r5 = acc / 20, r6 = acc % 20   (0x02089228 / 0x02089320 are div/mod helpers)
02041456  movs r4,#0
0204145a  movs r0,#0xf8
0204145c  adds r4,#1
0204145e  bl   02037c1c           ; <-- send one clock per 20 units
02041462  cmp  r5,r4
02041464  bhi  0204145a
02041466  str  r6,[acc]
```

0x02041474 resets the accumulator (called with 0x14 right after Start so the
first clock follows immediately). The single caller of the sender,
0x020415B8, first calls getter 0x02040428 (`ldrb r0,[struct+8]`, a sync-mode
byte) and only sends clock when it is 0, which matches the documented
"follows external clock, resumes after 3 s" behaviour.

## The patch

`patch_clock.py` replaces the 4-byte `bl 0x02037C1C` at 0x0204145E
(file offset 0x2145E, bytes `F7 F6 FB DD`) with two Thumb NOPs
(`46 C0 46 C0`, `mov r8,r8`). Result: no Timing Clock is ever transmitted;
Start/Continue/Stop, Active Sensing, notes and everything else are
untouched. The patched image differs from stock in exactly those 4 bytes;
the rebuilt SMF differs from Yamaha's in 6 bytes (packed data + checksum).

**Flashed and verified on hardware 2026-10-19**: the DX accepted the patched image (no image-level checksum is enforced), boots normally, sends no 0xF8 at all, and still sends Active Sensing, notes, Start and Stop. See CHECKLIST.md. Flashing options used/available:

1. Copy `reface updater.app`, replace `Contents/Resources/reface updater.dat`
   with the patched `.mid`, and ad-hoc re-sign so macOS will launch it:
   `codesign --force --deep --sign - "reface updater.app"`.
2. Or send the SysEx messages yourself (e.g. python `mido`/`rtmidi`) with
   the unit in Update mode, keeping the ~1309-tick (about 0.68 s at the SMF
   tempo) gap between blocks that the updater uses.

A later version could gate the `bl` on the existing "MIDI control" system
flag instead of removing it, giving a user-facing switch.

## The MIDI 2 page patch (v2): a real menu switch

`tools/patch_clock_menu.py` replaces the permanent removal with a
user-facing setting: it adds a sixth FUNCTION page ("MIDI 2") holding one
on/off item, CLOCK, stored at reserved settings address `0x15` and gated at
the clock-sender call site via a small trampoline in free ROM.  Four sites
of stock data/code change: the page-group descriptor (5→6 pages, new list
pointer), the `bl` at 0x020415C8 → trampoline, the SYSTEM parameter entry
for 0x15 (max 0→1, so the setter stops clamping it to 0) and the UI
page-index entry (max 4→5).  Everything else is new code/data placed in the
0xFF fill from file 0x140004.  Semantics: byte 0x15 is a CLOCK-OFF flag,
1 = clock off, anything else = on; the firmware's own default-fill writes 0,
so out-of-the-box behaviour matches stock until the user turns it off.
The CLOCK item is column 0 and is toggled by the column-0 key (micom id
0x12); the page is reached by the stock FUNCTION page cycler, which honours
the descriptor's page count.

The full mechanism (page records, group descriptor, layouts, parameter
entries, LCD bitmap formats, event routing) is documented in
[MENU_RE.md](MENU_RE.md).  The patch is verified by `tools/verify_menu.py`
(unicorn): the generated code is driven through the real master key
dispatcher, the real parameter setter (after running the firmware's own
group registrar and hook installer), the real label-list walker and the
real page cycler, with stock pages as control cases; both trailer checksums
are checked against stock.  Not yet flashed on hardware — see CHECKLIST.md
Phase 3.  An earlier build of this patch (never flashed) would not have
worked: two independent reviews on 2026-10-20 found the 0x15 entry clamped
every value to 0, the page index clamped to 4, the toggle listened on a
key id that is not a column key, and the CLOCK bitmap sat on a sampled
checksum offset.

## Reported (not verified here)

- Yamaha staff on YamahaSynth: the DX always sends clock and Active Sensing;
  "MIDI control = off" does not stop them. No official way to disable.
- reface CS/DX main board: Yamaha SSP2 (uPD800500F1-011-KN9-A), 8 MB program
  flash on the CPU bus, 16 MB SDRAM, 8 MB DSP SDRAM, AK4396 DAC, PCM1803 ADC,
  YDA176 amp, Fujitsu MB9AF141LA Cortex-M3 for key/panel scanning. CS and DX
  are the same board; DX adds the LCD and touch strips.
- reface CP/YC main board: Yamaha SWX08 (R8A02042BG), Cypress S29GL256S NOR
  flash, 64 MB (CP) / 32 MB (YC) wave ROM, same Cortex-M3 panel MCU.
- The SSP2's "SH-2A core" is the blogger's inference; this image shows it is
  ARM. The panel MCU firmware is not part of this update file.

## Unknowns and risks

1. ~~Whether the bootloader checks the 8-byte trailer~~ Resolved: it accepted
   the patched image, so there is no image-level check that the patch trips.
2. Exact ARM core. Thumb-1 only was seen; treat as ARMv5T until proven otherwise.
3. The 9-byte header message. (The 8-byte trailer is now decoded: sampled
   lane sums + byte-sum, see above; whether the bootloader enforces the
   lane check is still unknown.)
4. Recovery: Update mode is entered by key combination before the main image
   runs, so a bad main image should be recoverable by reflashing stock. Not
   tested. Never touch the boot ROM.

## Plan

1. ~~Checksum probe on hardware (Unknown 1)~~ done 2026-10-19: no image-level check.
2. ~~Verify no 0xF8, Start/Stop still sent~~ done 2026-10-19 (USB).
3. ~~Menu switch instead of permanent removal~~ implemented (MIDI 2 page,
   `tools/patch_clock_menu.py`); hardware test pending — CHECKLIST Phase 3.
4. Optional: confirm DIN MIDI OUT matches USB on any variant.

## Sources

- https://www.yamahasynth.com/forum/reface-dx-stop-midi-clock-and-program-change-messages
- https://gearspace.com/board/electronic-music-instruments-and-electronic-music-production/1043861-yamaha-reface-yes-16.html
- http://sandsoftwaresound.net/inside-reface-dx-and-cs/
- http://sandsoftwaresound.net/inside-reface-yc-and-cp/
- https://sandsoftwaresound.net/ssp1-and-ssp2/
- https://usa.yamaha.com/support/updates/reface_csdx_updater_for_mac.html
- https://www.manualslib.com/manual/2298170/Yamaha-Reface-Cs.html?page=4
- https://data.yamaha.com/files/download/other_assets/0/960060/v130_Manual_en.pdf
