# Hardware test checklist: reface DX no-clock firmware

Two re-signed copies of Yamaha's updater live under `reface_csdx_fw/updater/`
(built by `tools/make_updaters.sh`, git-ignored):

| app | payload | purpose |
|---|---|---|
| `updater/stock/reface updater.app` | byte-identical to Yamaha's v1.30 | proves the re-signed app + our repacked SMF path flashes cleanly |
| `updater/noclock/reface updater.app` | 4-byte patch at 0x0204145E | the permanent no-clock mod |
| `updater/clockmenu/reface updater.app` | MIDI 2 page patch (`tools/patch_clock_menu.py`) | the menu switch mod |
| `updater/orig/.../reface updater.app` | untouched Yamaha app | recovery |

Payload SHA-256: stock `645309be…2abb`, noclock `af1d2453…4799`, clockmenu `7f67dfd0…a6bf`.

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

Built and emulation-verified, **not yet flashed**.  Read MENU_RE.md §8 for
what the patch changes before running it.  The app is
`updater/clockmenu/reface updater.app` (payload SHA-256
`7f67dfd0…a6bf`).

1. Same Update-mode entry (F#3 + A3 + C#4 at power-on), flash, expect the
   same display sequence and 3-ish minute transfer.
2. First boot after flashing: the DX should behave exactly like stock
   (clock still on — the default is "on" until you change it).
3. FUNCTION menu: press FUNCTION repeatedly — the pages should now cycle
   through six (Voice, Tuning, MIDI, System, Job, MIDI 2).  If cycling stops
   at Job, try FUNCTION + keyboard keys hunting for the one that jumps to
   MIDI 2 (quick-key id 0x17 — the exact key was not identified offline;
   note which key worked in the Record below).
4. On MIDI 2: title should show the MIDI icon, one item labelled "CLOCK"
   with an OFF/ON switch graphic, value showing ON (default).
5. Toggle it off: MIDI monitor should show F8 stop immediately (FE Active
   Sensing continues).  Toggle on: F8 resumes.  Opening the page the first
   time and each toggle go through the stock parameter setter, so the
   monitor may also show a short Yamaha SysEx parameter change for system
   address 0x15 (the same thing the MIDI page does for CONTROL / LOCAL);
   that is expected, not a fault.  If a MIDI monitor is not
   handy, feed the DX external clock first so its own sender is idle, then
   check with a DAW.
6. Leave FUNCTION mode, power-cycle, re-enter FUNCTION: the CLOCK setting
   and the clock behaviour should persist.  If it does not persist, that
   means the settings flash save skips reserved bytes — record it; the
   toggle still works per session.
7. Regression pass: the other five pages draw and work as before; voice
   edits, Looper Start/Stop, notes all normal.
   - Known deviation to confirm: select a POLY voice and open the Voice
     page.  Stock leaves column 3 empty; the patched build is expected to
     show Portamento Time there (page-0 layout frozen on the mono layout,
     MENU_RE §8).  Check that a MONO voice shows the same four items as
     stock and that editing still works in both modes.
   - The quick-key range widening also routes key id 0x16 to the Job page
     (stock never page-jumped on 0x16).  While hunting for the 0x17 key,
     note any key that unexpectedly jumps to Job and whether that bothers
     normal FUNCTION use.
8. If anything misbehaves on the MIDI pages, reflash `updater/stock` or
   `updater/orig` — the patch only changes three code bytes plus free ROM.

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
