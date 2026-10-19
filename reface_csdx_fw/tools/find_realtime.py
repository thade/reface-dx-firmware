#!/usr/bin/env python3
"""Locate MIDI real-time message senders in the decoded reface CS/DX image.

Big-endian Thumb-1 code, link base 0x02020000.  For every `movs rX,#imm`
with imm in {F8,FE,FA,FB,FC}, record the next BL target; call targets shared
between F8 and FE sites are the real-time transmit path.

Usage: find_realtime.py <image.bin> [--callers 0xADDR] [--dis 0xADDR[:len]]
"""
import sys, struct, collections
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_BIG_ENDIAN

BASE = 0x02020000
CODE_END = 0x80000
NAMES = {0xF8: "clock", 0xFA: "start", 0xFB: "continue", 0xFC: "stop", 0xFE: "active-sense"}

def hw(d, off):
    return struct.unpack_from(">H", d, off)[0]

def bl_target(d, off):
    """If a 32-bit Thumb BL starts at off, return its target address else None."""
    h1, h2 = hw(d, off), hw(d, off + 2)
    if (h1 & 0xF800) == 0xF000 and (h2 & 0xF800) == 0xF800:
        imm = ((h1 & 0x7FF) << 12) | ((h2 & 0x7FF) << 1)
        if imm & 0x400000:
            imm -= 0x800000
        return BASE + off + 4 + imm
    return None

def all_bl_sites(d):
    sites = []
    for off in range(0, CODE_END, 2):
        t = bl_target(d, off)
        if t is not None and BASE <= t < BASE + CODE_END:
            sites.append((off, t))
    return sites

def imm_sites(d, imm):
    out = []
    for off in range(0, CODE_END, 2):
        h = hw(d, off)
        if (h & 0xF8FF) == (0x2000 | imm):          # movs rX, #imm
            out.append((off, (h >> 8) & 7))
    return out

def next_bl(d, off, window=24):
    """Address of first BL within `window` bytes after off; also whether a BX/POP intervenes."""
    for o in range(off + 2, off + window, 2):
        t = bl_target(d, o)
        if t is not None:
            return t, o
        h = hw(d, o)
        if h == 0x4770 or (h & 0xFF00) == 0xBD00:      # bx lr / pop {..pc}
            return None, o
    return None, None

def disasm(d, off, length=0x60, mark=()):
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_BIG_ENDIAN)
    for i in md.disasm(d[off:off + length], BASE + off):
        m = "   <--" if i.address in mark else ""
        print("  %08x  %-6s %s%s" % (i.address, i.mnemonic, i.op_str, m))

def main(argv):
    d = open(argv[1], "rb").read()
    if "--dis" in argv:
        a = argv[argv.index("--dis") + 1]
        addr, _, ln = a.partition(":")
        addr = int(addr, 16); ln = int(ln, 16) if ln else 0x80
        disasm(d, addr - BASE if addr >= BASE else addr, ln)
        return 0
    bl_sites = all_bl_sites(d)
    callers = collections.defaultdict(list)
    for off, t in bl_sites:
        callers[t].append(off)
    if "--callers" in argv:
        t = int(argv[argv.index("--callers") + 1], 16)
        print("callers of %08x:" % t)
        for off in callers.get(t, []):
            print("  bl at %08x (file %#x)" % (BASE + off, off))
        return 0
    per_imm = {}
    for imm, name in NAMES.items():
        rows = []
        for off, reg in imm_sites(d, imm):
            t, at = next_bl(d, off)
            rows.append((off, reg, t))
        per_imm[imm] = rows
    tally = collections.defaultdict(lambda: collections.defaultdict(list))
    for imm, rows in per_imm.items():
        for off, reg, t in rows:
            if t:
                tally[t][imm].append((off, reg))
    print("BL targets reached right after a real-time constant load, by how many distinct constants:")
    ranked = sorted(tally.items(), key=lambda kv: (-len(kv[1]), -sum(len(v) for v in kv[1].values())))
    for t, by in ranked[:15]:
        desc = ", ".join("%s x%d" % (NAMES[i], len(v)) for i, v in sorted(by.items()))
        print("  %08x  (%d callers total)  %s" % (t, len(callers.get(t, [])), desc))
        for i, v in sorted(by.items()):
            for off, reg in v:
                print("      %s at %08x (r%d)" % (NAMES[i], BASE + off, reg))
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
