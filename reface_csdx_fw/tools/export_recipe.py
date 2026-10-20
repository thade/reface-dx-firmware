#!/usr/bin/env python3
"""Write the browser patcher's recipes (patcher/recipes/*.json).

A recipe holds only this project's own bytes: the free-ROM code and data and
the handful of in-place patches, as base64 `put` operations, plus the rules
for the two things the browser rebuilds from the user's own stock file (the
five title bars with six page dots, and the "MIDI 2" title made from the
stock MIDI glyphs).  It never contains Yamaha's firmware.  patcher.js applies
a recipe exactly as patch_clock_menu.py would and refuses any result whose
SHA-256 differs from the one recorded here.

Usage: export_recipe.py <stock updater.dat> <out dir>
"""
import sys, os, json, base64, hashlib, struct
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import refacefw, patch_clock, patch_clock_menu as pm

def sha(b): return hashlib.sha256(b).hexdigest()
def b64(b): return base64.b64encode(bytes(b)).decode()

def diff_ops(stock, img, skip):
    """put ops for every byte that differs, merged into runs, skipping ranges in `skip`."""
    ops = []; i = 0; n = len(stock)
    def skipped(x): return any(a <= x < b for a, b in skip)
    while i < n:
        if stock[i] != img[i] and not skipped(i):
            j = i
            while j < n and not skipped(j) and (stock[j] != img[j] or (j + 2 < n and (stock[j+1] != img[j+1] or stock[j+2] != img[j+2]))):
                j += 1
            ops.append(["put", i, b64(img[i:j])]); i = j
        else:
            i += 1
    return ops

def clock_switch(stock_img):
    img, report = pm.patch(stock_img)
    P = {}
    import re
    for m in re.finditer(r"^\s+(.+?)\s+(0x[0-9a-f]+) \(file", report, re.M):
        P[m.group(1).strip()] = int(m.group(2), 16) - pm.BASE
    titles = [struct.unpack_from(">I", stock_img, pm.TITLE_TABLE - pm.BASE + 0x24 + 4 * i)[0] - pm.BASE for i in range(5)]
    tsize = pm.TITLE_W * pm.TITLE_H // 8
    skip = [(t, t + tsize) for t in titles] + [(P["MIDI 2 title"], P["MIDI 2 title"] + tsize)]
    return img, {
        "ops": diff_ops(stock_img, img, skip),
        "titles": {"at": titles, "width": pm.TITLE_W, "height": pm.TITLE_H, "pages": pm.NPAGES, "shift": 6,
                   "dots": {"x0": pm.DOT_X0, "pitch": pm.DOT_PITCH, "y": pm.DOT_Y, "filled": pm.DOT_FILLED, "empty": pm.DOT_EMPTY}},
        "midi2_title": {"at": P["MIDI 2 title"], "from_title": 2, "columns": [55, 75], "glyph": {"rows": pm.GLYPH_2, "x": 77, "y": 3}},
    }

def no_clock(stock_img):
    img = bytearray(stock_img)
    off = patch_clock.BL_ADDR - patch_clock.BASE
    img[off:off + 4] = patch_clock.NOP2
    return bytes(img), {"ops": diff_ops(stock_img, bytes(img), [])}

def main(argv):
    dat = open(argv[1], "rb").read(); out = argv[2]
    hdr, stock_img = refacefw.extract(dat)
    assert refacefw.repack(stock_img, hdr) == dat, "stock updater.dat does not round-trip"
    recipes = {
        "clock-switch": ("Clock switch (MIDI 2 page)", clock_switch),
        "no-clock": ("Clock always off", no_clock),
    }
    index = {"current": "clock-switch", "versions": list(recipes)}
    for key, (title, fn) in recipes.items():
        img, body = fn(stock_img)
        payload = refacefw.repack(img)
        rec = {"format": 1, "version": key, "title": title, "built_from": "reface CS/DX updater v1.30",
               "stock_sha256": sha(dat), "stock_image_sha256": sha(stock_img), "image_size": len(stock_img),
               "image_sha256": sha(img), "payload_sha256": sha(payload), "payload_size": len(payload),
               "smf": {"ppqn": 480, "tempo_us": 500000, "block_delta": refacefw.BLOCK_DELTA}}
        rec.update(body)
        with open(os.path.join(out, key + ".json"), "w") as f:
            json.dump(rec, f, indent=1)
        print("%-13s ops %3d  image %s  payload %s" % (key, len(rec["ops"]), rec["image_sha256"][:16], rec["payload_sha256"][:16]))
    with open(os.path.join(out, "index.json"), "w") as f:
        json.dump(index, f, indent=1)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
