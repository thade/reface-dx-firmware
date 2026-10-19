#!/usr/bin/env python3
"""Patch the reface CS/DX v1.30 image so it never transmits MIDI Timing Clock.

The clock sender (link address 0x02041438) accumulates timer ticks and calls
the real-time transmit function 0x02037C1C with r0 = 0xF8 once per 20 ticks:

    0204145a  movs r0, #0xf8
    0204145c  adds r4, #1
    0204145e  bl   0x2037c1c      <- replaced by two NOPs (mov r8, r8)
    02041462  cmp  r5, r4
    02041464  bhi  0x204145a

Start (0xFA), Continue (0xFB), Stop (0xFC) and Active Sensing use other call
sites and are unaffected.

Usage: patch_clock.py <image.bin> <patched.bin> [<patched.mid>]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refacefw

BASE = 0x02020000
BL_ADDR = 0x0204145E
BL_TARGET = 0x02037C1C
NOP2 = bytes.fromhex("46c046c0")          # mov r8,r8 ; mov r8,r8 (big-endian Thumb)

def decode_bl(b):
    h1 = (b[0] << 8) | b[1]; h2 = (b[2] << 8) | b[3]
    assert (h1 & 0xF800) == 0xF000 and (h2 & 0xF800) == 0xF800, "not a BL"
    imm = ((h1 & 0x7FF) << 12) | ((h2 & 0x7FF) << 1)
    if imm & 0x400000:
        imm -= 0x800000
    return imm

def main(argv):
    img = bytearray(open(argv[1], "rb").read())
    off = BL_ADDR - BASE
    orig = bytes(img[off:off + 4])
    expect_ctx = bytes.fromhex("20f8")      # movs r0,#0xf8 just before
    assert img[off - 4:off - 2] == expect_ctx, "context mismatch: wrong image?"
    if orig == NOP2:
        print("already patched")
    else:
        assert BL_ADDR + 4 + decode_bl(orig) == BL_TARGET, "BL does not target the transmit function: %s" % orig.hex()
        img[off:off + 4] = NOP2
        print("patched BL at %08x (file %#x): %s -> %s" % (BL_ADDR, off, orig.hex(), NOP2.hex()))
    open(argv[2], "wb").write(img)
    if len(argv) > 3:
        open(argv[3], "wb").write(refacefw.repack(bytes(img)))
        print("wrote updater payload", argv[3])
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
