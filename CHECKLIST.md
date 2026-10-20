# Hardware test checklist: reface DX no-clock firmware

Three re-signed copies of Yamaha's updater live under `reface_csdx_fw/updater/`
(built by `tools/make_updaters.sh`, git-ignored):

| app | payload | purpose |
|---|---|---|
| `updater/stock/reface updater.app` | byte-identical to Yamaha's v1.30 | proves the re-signed app + our repacked SMF path flashes cleanly |
| `updater/noclock/reface updater.app` | 4-byte patch at 0x0204145E | the permanent no-clock mod |
| `updater/clockmenu/reface updater.app` | MIDI 2 page patch (`tools/patch_clock_menu.py`) | the menu switch mod |
| `updater/orig/.../reface updater.app` | untouched Yamaha app | recovery |

Payload SHA-256: stock `645309be…2abb`, noclock `af1d2453…4799`, clockmenu `8b7a197d…bb23`.

**Result: both phases passed on 2026-10-19. See Record at the bottom.**

## Before you start

- [ ] reface DX on its AC adaptor, not USB power. Yamaha's guide insists on this.
- [ ] Direct USB cable to the Mac, no hub. Close every other MIDI app (DAW, MIDI monitor, Soundmondo, Reface Capture) so nothing else grabs the port.
- [ ] Mac on power, sleep disabled for the session (`caffeinate -d` in a terminal is enough).
- [ ] Note current firmware: hold the lowest key (C2) at power-on; v1.30 lights the LOOPER REC lamp (per forum reports). Or check the FUNCTION screen if it shows a version.
- [ ] Back up your 32 voices: Reface Capture or Soundmondo, or a SysEx bulk dump. The stock updater does not erase them, but the hardware test is the one time you want a backup.
- [ ] Have a MIDI monitor ready for afterwards (MIDI Monitor.app on macOS, or `receivemidi`).

## Phase 1: stock rebuild (proves the pipeline)

1. Power the DX off. Hold **F#3 + A3 + C#4** and press Standby/On. Display should read "reface DX Update". If it boots normally instead, you missed a key; power off and retry.
2. Open `updater/stock/reface updater.app`. First launch on this Mac may need right-click > Open because it is ad-hoc signed, and it runs under Rosetta.
3. Follow the app: it should find the device and show progress. Yamaha says ~5 minutes; the SMF is 139 blocks with ~0.7 s gaps, so expect 2 to 5 minutes.
4. Do not touch the cable, the Mac, or the DX until the app says it is finished and the DX restarts.
5. Expected result: DX boots normally, same version, voices intact.
   - If the updater reports an error before sending: USB/driver issue, not firmware. Retry with a different cable/port.
   - If it sends everything and the DX refuses to boot or stays in Update mode: the bootloader rejected something in our SMF even though it is byte-identical. Reflash with `updater/orig`. That would be surprising; report the exact display text.

## Phase 2: no-clock image

1. Same Update-mode entry (F#3 + A3 + C#4 at power-on).
2. Open `updater/noclock/reface updater.app`, run it to completion.
3. Expected results:
   - DX boots normally. If it does not, the bootloader verifies an image-level checksum: reflash stock (Phase 1 app or `orig`), then we go hunting for the trailer CRC.
   - Play notes: sound and MIDI note out still work.
   - MIDI monitor on the USB port: **no 0xF8 Timing Clock** at any time, with the Looper stopped and with it playing.
   - Active Sensing (0xFE every ~200 ms) is still present. That is expected and untouched.
   - Looper: press play and stop, confirm Start (0xFA) / Stop (0xFC) are still sent. Continue (0xFB) too if you pause/resume.
   - DIN MIDI OUT: same checks if you have a DIN interface. The transmit wrapper feeds both ports, so DIN should match USB.
   - External clock in: feed clock from the DAW and confirm the Looper still follows it (receive path is untouched).
4. Power-cycle and re-check once; settings and voices should persist.

## Phase 3: MIDI 2 page (the menu switch)

Built and emulation-verified (`tools/verify_menu.py` and `tools/verify_ui.py`:
the real parameter setter, the real UI event path from the FUNCTION button
to the flash save, and the rendered screen), **not yet flashed**.  Read MENU_RE.md §8 for what the patch changes before
running it.  The app is `updater/clockmenu/reface updater.app` (payload
SHA-256 `8b7a197d…bb23`).  The 2026-10-20 reviews found and fixed four
blockers in the earlier build (clamped setting, clamped page index, wrong
key id, trailer checksum); do not flash any clockmenu payload with a
different hash.

Semantics: settings byte 0x15 is a CLOCK-OFF flag.  1 = clock off, anything
else = clock on.  The firmware's own default-fill writes 0, so the unit
behaves like stock until you turn the clock off.

1. Same Update-mode entry (F#3 + A3 + C#4 at power-on), flash, expect the
   same display sequence and 3-ish minute transfer.
2. First boot after flashing: the DX should behave exactly like stock
   (F8 clock present on the MIDI monitor).
3. Press FUNCTION repeatedly: Voice → Tuning → MIDI → System → Job →
   **MIDI 2** → Voice.  (Emulation of the real UI code shows the FUNCTION
   button is micom key 0x0B and its cycler honours the new page count.)
4. On MIDI 2: the title bar reads "MIDI" with the MIDI icon (the new page
   reuses it), one item labelled "CLOCK" in column 0 with the ON graphic,
   columns 1 to 3 blank.
5. Toggle it with the **leftmost DATA ENTRY switch** (the one that toggles
   AUTO P.OFF on the System page).  MIDI monitor: F8 stops immediately (FE
   Active Sensing continues).  Toggle again: F8 resumes.
   **Expected to fail on builds up to and including `63a7154c…c847`:**
   emulation of the firmware's store receiver (MENU_RE §2.3,
   `verify_ui.py` "store receiver") shows the toggle's parameter message is
   dropped — the value should stay ON and F8 should keep running, with no
   flash write.  If that is what you see, it confirms the emulation; the
   fix is a synchronous store in the page-5 key handler.  Each toggle goes
   through the stock parameter setter, so the monitor may also show a short
   Yamaha SysEx parameter change for system address 0x15, the same thing
   the MIDI page does for CONTROL / LOCAL; that is expected, not a fault.
6. Set CLOCK off, then **leave FUNCTION** (press any other panel button,
   e.g. one of the voice-edit buttons) — that is when the settings block is
   written to flash (MENU_RE §9).  Power-cycle, re-enter FUNCTION: the
   CLOCK setting and the clock behaviour should persist, and FUNCTION should
   come back on the page you left.  Switching off while still on the
   FUNCTION screen loses the change, exactly as for any stock setting.
7. Regression pass: the other five pages draw and work as before; voice
   edits, Looper Start/Stop, notes all normal.
   - Known deviation to confirm: select a POLY voice and open the Voice
     page.  Stock leaves column 3 empty; the patched build is expected to
     show Portamento Time there (page-0 layout frozen on the mono layout,
     MENU_RE §8).  Check that a MONO voice shows the same four items as
     stock and that editing still works in both modes.
   - Leave FUNCTION while on MIDI 2 and re-enter: it should come back on
     MIDI 2 (the page-index parameter now allows 5).
8. If anything misbehaves, reflash `updater/stock` or `updater/orig` — the
   patch changes four in-place sites (descriptor, one `bl`, two parameter
   entry words) plus free ROM, and keeps both trailer checksums equal to
   stock.

## Recovery

- Update mode is entered from the keyboard before the main image runs, so a bad main image is recoverable: hold F#3 + A3 + C#4 at power-on and flash `updater/orig/reface_updater_CSDX_v130-3_mac/reface updater.app` (Yamaha's untouched app).
- If the unit will not enter Update mode at all, that would mean the boot ROM was affected. Our SMF writes only the same address range Yamaha's does (`03 50 00`), so this is not expected.

## Record

Write the outcome of each phase here with the display text and MIDI monitor observations, then commit.

- Phase 1 (stock rebuild), 2026-10-19: transfer 3:08 via re-signed `updater/stock` app.
  DX display went LOADING / ERASING / WRITING / "Completed. 1.30 --> 1.30 Reboot."
  Power-cycled; boots normally; 4 s listen: 192 x F8 (120 BPM), 20 x FE. Identical to pre-flash baseline.
- Phase 2 (no-clock), 2026-10-19: first attempt was sent while the DX was in normal mode (ignored, harmless).
  Second attempt with the DX in Update mode: transfer 3:08, same display sequence, "Completed. 1.30 --> 1.30 Reboot."
  Power-cycled; boots normally. 8 s listen: 0 x F8, 41 x FE (200 ms). Notes received; Looper play sent FA,
  Looper stop sent FC; no F8 at any point with the Looper running. Bootloader does not verify an image checksum
  (or the trailer covers a region the patch does not touch). USB verified; DIN not yet checked.
