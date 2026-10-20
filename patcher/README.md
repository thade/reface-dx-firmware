# Browser patcher

A static page that makes the clock-switch firmware from Yamaha's own reface
CS/DX v1.30 updater download and sends it to the DX, in the browser. No
Python, and it works on Windows. Nothing is uploaded, and the page contains
none of Yamaha's firmware.

- `index.html` and `page.js`: the page (what the firmware does, get Yamaha's
  file, accept the risks, drop the zip or `reface updater.dat`, then send the
  result to the DX or download it). `zip.js` reads the zip with the browser's
  own deflate decoder. The page loads no third-party code.
- `patcher.js`: the build, a JavaScript copy of
  [refacefw.py](../reface_csdx_fw/tools/refacefw.py) (the updater payload is
  a Standard MIDI File of SysEx blocks) and of the data side of
  [patch_clock_menu.py](../reface_csdx_fw/tools/patch_clock_menu.py). A
  recipe (`recipes/*.json`, written by
  [export_recipe.py](../reface_csdx_fw/tools/export_recipe.py)) holds only
  this project's bytes; the title bars are rebuilt from the user's own stock
  file. It refuses any input that is not the stock v1.30 `reface updater.dat`
  and any result that is not the published payload.
- `flasher.js`: send to the DX, in Chrome and Edge on a computer, over USB
  MIDI (Web MIDI, SysEx). It sends the same 139 messages with the same gaps
  as Yamaha's updater. The DX must already be in Update mode.

To try it locally: `python3 -m http.server -d patcher 8000` and open
http://localhost:8000/. Web MIDI needs Chrome or Edge.
