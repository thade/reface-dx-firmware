# Hardware test checklist: reface DX clock firmware

Three re-signed copies of Yamaha's updater live under `reface_csdx_fw/updater/`
(built by `tools/make_updaters.sh`, git-ignored):

| app | payload | purpose |
|---|---|---|
| `updater/stock/reface updater.app` | byte-identical to Yamaha's v1.30 | proves the re-signed app + our repacked SMF path flashes cleanly |
| `updater/noclock/reface updater.app` | 4-byte patch at 0x0204145E | the permanent no-clock mod |
| `updater/clockmenu/reface updater.app` | MIDI 2 page patch (`tools/patch_clock_menu.py`) | the menu switch mod |
| `updater/orig/.../reface updater.app` | untouched Yamaha app | recovery |

Payload SHA-256: stock `645309be…2abb`, noclock `af1d2453…4799`, clockmenu `1702d694…5709`
(flashed 2026-10-20; the full Phase 3 steps ran on an earlier build, see the Record).

**Result: Phases 1 and 2 passed on 2026-10-19, Phases 3 and 3b on 2026-10-20. See Record at the bottom.**

## Before you start

- [ ] reface DX on its AC adaptor. Yamaha's guide insists on this.
- [ ] Direct USB cable to the Mac, no hub. Close every other MIDI app (DAW, MIDI monitor, Soundmondo, Reface Capture) so nothing else grabs the port.
- [ ] Mac on power, sleep disabled for the session (`caffeinate -d` in a terminal is enough).
- [ ] Note current firmware: hold the lowest key (C2) at power-on; v1.30 lights the LOOPER REC lamp (per forum reports). Or check the FUNCTION screen if it shows a version.
- [ ] Back up your 32 voices: Reface Capture or Soundmondo, or a SysEx bulk dump. The stock updater does not erase them, but the hardware test is the one time you want a backup.
- [ ] Have a MIDI monitor ready for afterwards (MIDI Monitor.app on macOS, or `receivemidi`).

## Phase 1: stock rebuild (proves the pipeline)

1. Power the DX off. Hold **F#3 + A3 + C#4** and press Standby/On. Display should read "reface DX Update". If it boots normally instead, you missed a key; power off and retry.
2. Open `updater/stock/reface updater.app`. First launch on this Mac may need right-click > Open because it is ad-hoc signed, and it runs under Rosetta.
3. Follow the app: it should find the device and show progress. The SMF is 139 blocks about 1.4 s apart, so expect about 3 minutes.
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
to the flash save, and the rendered screen). **Flashed and verified on hardware 2026-10-20, see Record.**  Read menu-internals.md §8 for what the patch changes before
running it.  The app is `updater/clockmenu/reface updater.app` (payload
SHA-256 `1702d694…5709`).  The 2026-10-20 reviews and emulation found and
fixed six problems in earlier builds (clamped setting, clamped page index,
wrong key id, trailer checksum, dropped store, frozen Voice page layout);
do not flash any clockmenu payload with a hash other than the one pinned
in `tools/selftest.py`.  The current payload differs from the Phase 3b
payload `c5ea5147…f709` in two halfwords of the key handler (the push and
pop around the setter call now include r4, so the stack stays 8-byte
aligned at the call).  It passes the three emulation suites and was
flashed on 2026-10-20; see the Record for what was checked on it.

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
4. On MIDI 2: the title bar reads "MIDI 2" with six page dots (the sixth
   filled; every page now shows six dots), one item labelled "CLOCK" on one
   line in column 0 with the stock ON graphic, columns 1 to 3 blank.
5. Toggle it with the **leftmost DATA ENTRY switch** (the one that toggles
   AUTO P.OFF on the System page).  MIDI monitor: F8 stops immediately (FE
   Active Sensing continues).  Toggle again: F8 resumes.
   Each toggle goes through the stock parameter setter and then stores the
   byte directly (the firmware's own store path ignores address 0x15), so
   the monitor may also show a short
   Yamaha SysEx parameter change for system address 0x15, the same thing
   the MIDI page does for CONTROL / LOCAL; that is expected, not a fault.
6. Set CLOCK off, then **leave FUNCTION** (press any other panel button,
   e.g. one of the voice-edit buttons) — that is when the settings block is
   written to flash (menu-internals.md §9).  Power-cycle, re-enter FUNCTION: the
   CLOCK setting and the clock behaviour should persist.  After a power-cycle
   FUNCTION opens on Voice, because the page index is not saved.  Switching off while still on the
   FUNCTION screen loses the change, exactly as for any stock setting.
7. Regression pass: the other five pages draw and work as before; voice
   edits, Looper Start/Stop, notes all normal.
   - Voice page: with a POLY voice column 3 must be empty, with a MONO
     voice it must show Portamento Time, exactly as stock (an earlier build
     broke this; the fix is verified by emulation, menu-internals.md §8).
   - Leave FUNCTION while on MIDI 2 and re-enter: it should come back on
     MIDI 2 (the page-index parameter now allows 5).
8. If anything misbehaves, reflash `updater/stock` or `updater/orig` — the
   patch changes five in-place sites (descriptor, two `bl`, two parameter
   entry words), rewrites the five title bitmaps, adds free ROM, and keeps
   both trailer checksums equal to stock.

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
- Phase 3 (MIDI 2 page, payload `b0e728ef…fddd`), 2026-10-20: transfer 3:04 via re-signed `updater/clockmenu` app,
  DX showed "Completed. 1.30 --> 1.30 Reboot." Power-cycled; boots normally. First probe looked wrong (1 x F8 every
  3.0 s) because an external clock at 76 BPM was driving the DX, which silences its own clock by design; with the
  external clock stopped: 384 x F8 in 8 s (120.0 BPM), 41 x FE (196 ms), no other traffic. Steps 1-3 pass.
  Step 4: FUNCTION cycles Voice, Tuning, MIDI, System, Job, MIDI 2 (CLOCK item). Step 5: leftmost DATA ENTRY
  switch set CLOCK to off; probe: 0 x F8 in 8 s, 41 x FE (196 ms). The switch works.
  Toggled back on: 384 x F8 in 8 s (120.0 BPM) immediately. Toggle works both ways.
  Step 6 FAILED: CLOCK off, left FUNCTION via a voice-edit button, power-cycled -> 384 x F8 in 8 s (clock back on).
  Either the save did not run on leaving FUNCTION or boot reset byte 0x15. Investigating.
  Step 6 re-run with the SysEx dump request as the readout: CLOCK off on the panel -> live byte 0x15 = 1, 0 x F8;
  left FUNCTION (lamp out), standby-cycled -> byte 0x15 still 1, 0 x F8 in 8 s, FE normal. Persistence works.
  The earlier failure: both CONTROL and CLOCK reverted, so no save had run; most likely Function mode was not
  actually exited before the power-cycle. Your own TX ch 13 / RX ch 16 / auto power-off settings were intact
  throughout.
  Step 7: after the power-cycle FUNCTION opens on Voice (page index is RAM state, same as stock); Voice page with a
  POLY voice shows an empty column 3 (Part Mode layout swap intact). Phase 3 passes: the MIDI 2 CLOCK switch works,
  persists, and the other pages behave as stock.
- Phase 3b (display fixes, payload `c5ea5147…f709`), 2026-10-20: first transfer ran 3:03 but the DX stayed at
  "98%" because the updater app was quit a second after its timer stopped, before the last blocks had been
  delivered. Lesson: never quit the updater until the DX display itself shows "Completed". Recovery per Yamaha's
  guide: repeat the update from the beginning (re-enter Update mode).
  Repeat from Update mode: on relaunch the updater had silently defaulted its port to another interface (the
  Zoom L6) after the DX re-enumerated; aborted that, relaunched, confirmed "reface DX" in the port menu, transfer
  3:08, DX showed "Completed. 1.30 --> 1.30 Reboot." Lesson: check the port menu every launch.
  After reboot: MIDI 2 shows the "MIDI 2" title, six dots with the sixth filled, CLOCK on one line (user-confirmed).
  SysEx dump: byte 0x15 = 1 and MIDI control = 1 as set before the reflash (the update leaves the settings
  sector alone); 0 x F8 in 8 s, 41 x FE. Phase 3b passes. Display: ON/off cells are the stock switch graphics.
- Payload `1702d694…5709`, 2026-10-20: built after a review found the key handler called the parameter setter
  with the stack 4-byte aligned (push {r3,lr} then push {r2}); the push and pop now include r4. Two halfwords
  differ from `c5ea5147…f709`, trailer lanes and byte-sum still equal stock, verify_menu, verify_ui and
  verify_paths all pass. Flashed on 2026-10-20.
  Clock receive with CLOCK off: the Mac sent 0xF8 at 100 BPM to the DX over USB (mido), 800 clocks in 20 s and
  then 1,200 in 30 s. The DX sent no F8 back (only note messages from keys played), and during the 30 s run the
  Looper tempo showed EXT (user-confirmed). So CLOCK off stops the DX's own clock and the DX still follows an
  external one. This matches the code: the receive handler 0x0203DADC and the Looper's EXT check read the sync
  mode, not byte 0x15, and an emulation of receive with byte 0x15 = 0 and 1 gave the same result. Before this
  test, EXT had been seen only with CLOCK on in the user's own setup; the cause there was not determined.
  Phase 3 steps 4-7 were not re-run on this payload.
- No-clock payload `af1d2453…4799` (unchanged since Phase 2), reflashed 2026-10-20 after the clockmenu tests.
  10 s USB listen with timing and Active Sensing unfiltered: 0 x F8, 51 x FE (196 ms), no other traffic.
  Looper Start/Stop, notes and DIN were not re-checked.
- Restore over the no-clock payload `af1d2453…4799`, 2026-10-20: Yamaha's unmodified `reface updater.dat`
  (`645309be…2abb`) sent with a Python rtmidi sender on the file's own timing (139 messages, 189.6 s), not
  Yamaha's app. The DX showed "Completed. 1.30 --> 1.30 Reboot." After a normal power-on the first 10 s USB
  listen showed 0 x F8 because an external clock was driving the DX (Looper tempo showed EXT). With that clock
  stopped: 480 x F8 in 10 s (120.0 BPM), 51 x FE. Yamaha's firmware is back.
- Clock switch payload `1702d694…5709` installed with the patcher page's Send, 2026-10-20. Before the next
  test: 0 x F8 in 8 s, 41 x FE, byte 0x15 = 1 (CLOCK off, kept from earlier).
- Restore with Yamaha's app over `1702d694…5709`, 2026-10-20: the untouched Yamaha-signed `reface updater.app`
  from the v1.30 zip, port "reface DX", Start. Transfer 3:08, DX showed "Completed. 1.30 --> 1.30 Reboot."
  The app was left open until then. After a normal power-on with no external clock: 481 x F8 in 10 s
  (120.3 BPM), 51 x FE. Yamaha's updater puts Yamaha's firmware back over this project's firmware.
