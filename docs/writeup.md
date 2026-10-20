# Yamaha reface DX: what we know so far

Status as of 2026-10-19. Everything below the "Reported" heading is secondhand;
everything above it was checked against the actual files.

## The question

The reface DX transmits MIDI Timing Clock (0xF8) continuously and Yamaha
provides no way to turn it off. We wanted to know whether a firmware patch is
realistic, and separately whether the reface CP firmware could run on DX
hardware.

## Short answers

- **Clock-off patch: done and verified on the DX (2026-10-19).** The firmware
  is unencrypted, uncompressed, big-endian ARM/Thumb. The clock sender is a
  60-byte function with one caller, and the patch is a 4-byte NOP of its single
  transmit call. Flashed through Yamaha's own updater with the payload swapped:
  no 0xF8 on USB at all, Active Sensing, notes, Looper Start/Stop unchanged.
- **Menu switch (v2): flashed and verified on the DX (2026-10-20).** A second patch
  (`tools/patch_clock_menu.py`) adds a sixth FUNCTION page, "MIDI 2", with a
  CLOCK on/off item stored at reserved settings address 0x15 (1 = off,
  default 0 = on). Five in-place sites change (page-descriptor count byte,
  a `bl` hook where page-apply computes the record address, the clock
  sender's `bl`, two parameter-table words to lift clamps); everything else
  is new code in free ROM, and the stock page list stays in RAM so the
  Voice page's poly/mono layout switch keeps working. Two independent reviews on 2026-10-20 found the first build
  would not have worked (setting clamped to 0, wrong key id, trailer
  checksum broken); the fixed build is verified by emulating the real
  parameter engine, key dispatcher and page cycler. See menu-internals.md.
- **CP firmware on a DX: no.** Different Yamaha chip (SWX08 vs SSP2),
  different board, and the CP needs a 64 MB sample ROM the DX does not have.
  The only physically possible swap is CS firmware on a DX, and that is the
  same binary already; the model is chosen at runtime by hardware detection.

## Is there really no setting?

Checked the reface Data List (MIDI implementation chart and the SysEx system
parameter table), the v1.30 Supplementary Manual (every added function and the
full keyboard-settings appendix), Yamaha's download pages, and the forums.

- Implementation chart: Clock and Commands transmitted, unconditionally. Only
  the controller CCs carry a "if MIDI Control is on" footnote.
- SysEx system table addresses 0x00 to 0x0E cover transmit/receive channel,
  tune, local, transpose, tempo, contrast, sustain, auto power-off, speaker,
  MIDI Control. 0x0F to 0x1F are reserved. No clock entry; the firmware's
  menu string table has none either.
- v1.30 (March 2016) is the last firmware. Later "1.30-2/-3" releases changed
  only the Mac updater for newer macOS versions.
- Yamaha staff on YamahaSynth: clock is always sent and cannot be turned off.
- Only documented behaviour that silences it: feed the DX external clock, and
  it follows that instead, resuming its own after 3 s of silence.

Non-firmware workarounds are a DIN filter (MIDI Solutions Event Processor or
a microcontroller passthrough) or host-side filtering for USB. There is no
clean USB fix without firmware.

## The update file

Yamaha's updater is a small Mac/Windows app plus a single payload,
`reface updater.dat`. It turned out to be a Standard MIDI File of 139 Yamaha
bulk-dump SysEx messages. The app is a SysEx player; it streams the file to
the unit over USB MIDI while the unit sits in Update mode (hold F#3 + A3 + C#4
at power-on).

- Framing: `F0 43 00 7F 1C <14-bit count> 03 50 00 <data> <checksum> F7`,
  standard Yamaha checksum (two's complement of address + data, 7-bit).
- Data is 7-bit packed: one MSB byte then seven data bytes. Each message
  carries a 5-byte sub-header with the block number and a length field equal
  to raw bytes + groups.
- Decoded: exactly 2 MB minus erase fill; 1.25 MB of live program.
- Build stamp: `REFACE MAIN 1.30 2016MAR03 144105`.
- One unified CS/DX binary (both model strings, both engines' source paths).
- No encryption or compression. Source file paths for about 80 modules are
  compiled in, which made the MIDI code easy to locate.
- Eight bytes after the build stamp are two checksums: four lane byte-sums
  over sampled offsets of each 512 KB chunk (reproduced exactly, see
  README), and the image's 8-bit byte-sum. The no-clock patch leaves the
  sampled lanes intact and the unit accepted its changed byte-sum, so the
  byte-sum is not enforced at boot; whether the lane check is enforced is
  unknown, so the menu patch keeps both equal to stock.

`tools/refacefw.py` extracts the image and rebuilds the SMF byte-identically
from it (verified with `roundtrip`).

## The CPU is not what the teardowns say

The well-known teardown blog assumes the reface CS/DX main processor (Yamaha
SSP2) is an SH-2A. The firmware says otherwise: a big-endian ARM exception
vector table, an ARM-mode reset handler that writes CPSR, and Thumb-1 code
throughout, read as big-endian halfwords. The image links at RAM address
0x02020000 (896 words point at string starts and 694 of 863 odd pointers land
on Thumb prologues at that base; no other base comes close). For Ghidra:
raw binary, language ARM:BE:32:v5t, base 0x02020000.

## The clock sender

`tools/find_realtime.py` sweeps the whole image for `movs rX,#imm` with imm in
{F8, FA, FB, FC, FE} and records the BL that follows each. One function is
called from sites that load each of F8, FA, FB and FC into r0:
**0x02037C1C**, a one-byte real-time transmit wrapper that posts a type-9
message to the MIDI output queue. It has exactly four callers in the entire
image:

- Start/Continue, from one function at 0x020403F4
- Stop, from 0x02040418
- **Clock, from 0x0204145E inside 0x02041438**

The clock sender adds the caller's tick count to an accumulator and sends one
0xF8 per 20 units, keeping the remainder. Its only caller, 0x020415B8, first
reads a sync-mode byte and skips clock when the unit is following external
clock, which matches the documented behaviour.

## The patch

Replace the 4-byte `bl 0x02037C1C` at 0x0204145E (file offset 0x2145E,
`F7 F6 FB DD`) with two Thumb NOPs (`46 C0 46 C0`). Clock is never sent;
Start/Continue/Stop, Active Sensing, notes, SysEx and everything else are
untouched. `tools/patch_clock.py` applies it with context checks and emits
the patched updater SMF, which differs from Yamaha's in six bytes.

`tools/make_updaters.sh` unpacks Yamaha's updater, produces a `stock` copy
(payload byte-identical to Yamaha's, re-signed) and a `noclock` copy, pins
all three payload hashes, and checks for Rosetta since the app is x86_64. The
stock copy launches and logs that it loaded the replaced payload.

## Risks

- Resolved: the bootloader accepted the patched image. Update mode is
  entered from the keyboard before the main image runs, so a broken main
  image remains recoverable by reflashing Yamaha's untouched app.
- The patch is permanent for that firmware; a later version could gate the
  call on the existing "MIDI Control" flag to give a menu switch.
- Exact ARM core is unconfirmed (treat as ARMv5T); irrelevant to this patch.

## Next

Done on USB. Remaining: flash the MIDI 2 menu-switch build (CHECKLIST Phase
3) — which key selects page 6 and whether the setting survives power cycles
are the two hardware questions. Optional afterwards: confirm the DIN MIDI
OUT is equally quiet (same transmit path, so expected).

## Reported, not verified here

- Board details from sandsoftwaresound.net: CS/DX share one board (SSP2,
  8 MB program flash, 16 MB SDRAM, 8 MB DSP SDRAM, AK4396 DAC, PCM1803 ADC,
  Cortex-M3 panel scanner); CP/YC share a different board (SWX08, 64/32 MB
  wave ROM, same panel MCU). Both pairs differ mainly in panel and software.
- Forum reports of the clock behaviour and of the v1.30 version check
  (hold C2 at power-on, LOOPER REC lamp lit).
