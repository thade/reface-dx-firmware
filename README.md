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
  tools/parse_smf.py               parse the updater SMF, list SysEx blocks, verify checksums
  tools/extract.py                 unpack the 7-bit payload into a raw firmware image
  out/reface_updater_CSDX_v130.mid the untouched payload from the official updater
  out/reface_csdx_main_v130.bin    decoded raw image (1,966,083 bytes)
```

`out/` is git-ignored: the files are Yamaha's copyrighted firmware and are
reproducible from the official download. Regenerate with:

```bash
python3 tools/parse_smf.py "reface updater.dat" msgs.pkl
python3 tools/extract.py msgs.pkl out/reface_csdx_main_v130.bin
```

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
  `01 <blk hi> <blk lo> <len hi> <len lo>` then packed data. Blocks 1..137 carry
  16368 packed bytes each; the last carries 4536 (length field says 4533, so the
  last 3 are padding).
- 7-bit packing: groups of 8 bytes, first byte holds the MSBs, bit 6 belongs to
  the first of the following 7 data bytes, bit 0 to the last.
- Decoded image: 1,966,083 bytes (0x1E0003). Everything from 0x140000 up is 0xFF
  erase fill, so the live program is about 1.25 MB in a 2 MB flash region.
- Not encrypted, not compressed. Code-region entropy is 6.3 to 7.0 bits/byte,
  strings and source paths are plaintext, no compression signatures found.
- Build stamp at the end of the image: `REFACE      MAIN` then
  `1302016MAR03144105`, i.e. v1.30 built 2016-03-03 14:41:05.
- Both `reface DX` and `reface CS` strings are present, plus `reface DX TstPrg`.
  It is one unified CS/DX binary that selects the model at runtime, consistent
  with the shared updater and shared main board.
- Eight non-text bytes follow the build stamp
  (`EE 1C 3B FD 42 FF 58 FF`). They look like a CRC or sum but did not match
  CRC-32, Adler-32, CRC-16 CCITT, CRC-16 IBM, or 8/16/32-bit sums over any of the
  obvious ranges (whole image, image minus trailer, 0x1E0000, 0x140000).

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
  `0x0401xxxx`, RAM data at `0x0215xxxx`.

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

Best lead so far: function at file offset 0x531C4 (address 0x020731C4).
It allocates a 40-byte object, calls a probe, and fills bytes 2..5 with
`FC FC 00 F8` on one path or `FE FC FE FC` on the other, then registers the
object. Looks like per-port real-time message configuration. Start here, and
also look for a timer callback that enqueues a single 0xF8 byte 24 times per
beat into the `mdi_usb*` / `sio_if` transmit path.

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

1. Whether the bootloader checks the 8-byte trailer (or anything else) before
   accepting an image. Cheapest test: resend the stock SMF with one harmless
   byte changed and the SysEx checksum recomputed, and see if the unit boots.
2. Exact ARM core. Thumb-1 only was seen; treat as ARMv5T until proven otherwise.
3. The 9-byte header message and the 8-byte trailer encoding.
4. Recovery: Update mode is entered by key combination before the main image
   runs, so a bad main image should be recoverable by reflashing stock. Not
   tested. Never touch the boot ROM.

## Plan

1. Build a repacker (inverse of `extract.py`): raw image -> 7-bit blocks ->
   SysEx -> SMF, byte-identical to the original for an unmodified image.
2. Checksum probe on hardware (Unknown 1).
3. Locate the 0xF8 sender; minimal patch is NOP-ing the enqueue. Better patch
   gates it on the existing "MIDI control" flag or a new menu item.
4. Flash, verify with a MIDI monitor on both USB and DIN.

## Sources

- https://www.yamahasynth.com/forum/reface-dx-stop-midi-clock-and-program-change-messages
- https://gearspace.com/board/electronic-music-instruments-and-electronic-music-production/1043861-yamaha-reface-yes-16.html
- http://sandsoftwaresound.net/inside-reface-dx-and-cs/
- http://sandsoftwaresound.net/inside-reface-yc-and-cp/
- https://sandsoftwaresound.net/ssp1-and-ssp2/
- https://usa.yamaha.com/support/updates/reface_csdx_updater_for_mac.html
- https://www.manualslib.com/manual/2298170/Yamaha-Reface-Cs.html?page=4
- https://data.yamaha.com/files/download/other_assets/0/960060/v130_Manual_en.pdf
