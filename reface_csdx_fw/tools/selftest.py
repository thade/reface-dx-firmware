#!/usr/bin/env python3
"""Regression check for the reface CS/DX firmware tools, from the stock image alone.

Rebuilds the stock updater SMF, the no-clock payload and the MIDI 2 page payload
in memory and compares their SHA-256 against the pinned values.  Needs only the
Python standard library: no capstone, unicorn, Yamaha zip or Rosetta.

Usage: selftest.py [stock_image.bin]      (default: out/reface_csdx_main_v130.bin)
"""
import sys, os, hashlib, io, contextlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import refacefw, patch_clock, patch_clock_menu
check = refacefw.check

EXPECT = {
    "stock payload":     "645309bef54e9444ebdeeacaa42f727b0fd0bdcfc06992ca1adde30a16112abb",
    "noclock payload":   "af1d24539c2f064c192a0d2bc86c06bbfefdcabc1486b5bcefe9379817ce4799",
    "clockmenu payload": "1702d69485c1bf490184421b5b8112d9cd9af3a3fe6c6e25a2a779ca59bd5709",
}

def sha(b): return hashlib.sha256(b).hexdigest()

def main(argv):
    path = argv[1] if len(argv) > 1 else os.path.join(HERE, "..", "out", "reface_csdx_main_v130.bin")
    img = open(path, "rb").read()
    if len(img) != 0x1E0000:
        print("FAIL: image is %d bytes, expected 1966080" % len(img)); return 1
    got = {}
    got["stock payload"] = sha(refacefw.repack(img))
    # patch_clock works on a file; run its main into temp buffers via a bytearray copy
    nc = bytearray(img)
    off = patch_clock.BL_ADDR - patch_clock.BASE
    check(patch_clock.BL_ADDR + 4 + patch_clock.decode_bl(bytes(nc[off:off+4])) == patch_clock.BL_TARGET)
    nc[off:off+4] = patch_clock.NOP2
    got["noclock payload"] = sha(refacefw.repack(bytes(nc)))
    with contextlib.redirect_stdout(io.StringIO()):
        menu_img, _report, _placed = patch_clock_menu.patch(img)
    got["clockmenu payload"] = sha(refacefw.repack(menu_img))
    rc = 0
    # trailer checksums (see refacefw.sampled_lanes): the clockmenu image must
    # keep both stock values; noclock changes the byte-sum (hardware accepted it)
    lanes_ok = refacefw.sampled_lanes(menu_img) == refacefw.trailer_lanes(menu_img) == refacefw.sampled_lanes(img)
    sum_ok = refacefw.byte_sum(menu_img) == refacefw.byte_sum(img)
    print("%-18s %s  lanes %s, byte-sum %#04x%s" % ("clockmenu trailer", "OK  " if lanes_ok and sum_ok else "FAIL",
          refacefw.sampled_lanes(menu_img).hex(), refacefw.byte_sum(menu_img), "" if sum_ok else " (stock %#04x)" % refacefw.byte_sum(img)))
    rc |= not (lanes_ok and sum_ok)
    nl = refacefw.sampled_lanes(bytes(nc)) == refacefw.trailer_lanes(bytes(nc))
    print("%-18s %s  lanes %s, byte-sum %#04x (stock %#04x; not enforced by the bootloader, hardware-tested)" % ("noclock trailer",
          "OK  " if nl else "FAIL", refacefw.sampled_lanes(bytes(nc)).hex(), refacefw.byte_sum(bytes(nc)), refacefw.byte_sum(img)))
    rc |= not nl
    for name, want in EXPECT.items():
        ok = got[name] == want
        print("%-18s %s  %s" % (name, "OK  " if ok else "FAIL", got[name][:16] + ("" if ok else "  (expected %s...)" % want[:16])))
        rc |= not ok
    return rc

if __name__ == "__main__":
    refacefw.run(main)
