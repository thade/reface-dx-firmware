#!/usr/bin/env python3
"""Add a "MIDI 2" FUNCTION page to the reface DX v1.30 image with a MIDI-clock switch.

Stock v1.30 has five FUNCTION pages (Voice, Tuning, MIDI, System, Job).  This
patch adds a sixth page, "MIDI 2", holding one on/off item labelled CLOCK that
controls whether MIDI Timing Clock (0xF8) is transmitted.  The flag is stored
in the system settings block at SysEx-reserved address 0x15 and persists with
the normal settings save.  Semantics: 0 = clock off, 1 = clock on; any other
byte (e.g. 0xFF from erased flash) is treated as on and is normalised to 1 the
first time the page is opened.

How the firmware works (verified against v1.30, see WRITEUP.md):
  * FUNCTION pages are 20-byte records {u8 columns, 3 pad, be32 layout,
    be32 key_handler, be32 static_draw, be32 value_draw} in shared RAM at
    0x04011020, copied at boot from ROM 0x13D9D8.  The group descriptor
    {u8 count, u8 current, u16 pad, be32 list} at ROM 0x13DA3C -> RAM
    0x04011084 selects them: record = list + page*0x14.
  * A page's layout is 16-byte rows {addr_hi, addr_mid, addr_lo, x, type,...};
    the row bytes 0..2 are the 24-bit SysEx parameter address of the item and
    0x83 marks an empty column.  Type 4 is an on/off switch.
  * Settings live at RAM 0x041480A0, byte offset == SysEx address; the SYSTEM
    group covers all 32 addresses 0x00-0x1F (0x0F-0x1F are reserved on the DX
    and the CS uses only 0x0F/0x14 besides).
  * Key events: master dispatcher 0x02044CB4 forwards event 3 to the page's
    key handler; quick-access keys with micom ids 0x12-0x15 additionally jump
    straight to page id-0x12 (0x02044BD0).  We extend the id range to 0x17 so
    id 0x17 jumps to the new page 5.
  * The clock sender 0x02041438 is called from exactly one place,
    0x020415C8 inside 0x020415B8, when not following external clock.

Patch sites (file offsets):
  0x13DA3C  group descriptor {5, RAM list}   -> {6, new list in free ROM}
  0x24D72   quick-key range check cmp #3     -> cmp #5 (ids 0x16,0x17 -> pages 4,5)
  0x215C8   bl clock sender                  -> bl trampoline (checks the flag)

New code/data is placed in the 0xFF erase-fill from file 0x140000
(address 0x02160000) onwards.  Everything is big-endian Thumb-1.

Usage: patch_clock_menu.py <image.bin> <patched.bin> [<patched.mid>]
"""
import sys, os, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refacefw
try:
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_BIG_ENDIAN
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_BIG_ENDIAN)
except ImportError:          # verification listing only; patch works without
    md = None

BASE      = 0x02020000
FREE      = 0x02160000            # first free-ROM address (file 0x140000)
FREE_END  = 0x02160000 + 0x20000  # generous working limit

# ---- stock firmware landmarks (asserted before patching) --------------------
SETTINGS   = 0x041480A0           # system settings block (byte == SysEx addr)
CLOCK_FLAG = 0x15                 # reserved on both CS and DX
SET_PARAM  = 0x0204564C           # setter(param_addr, op=1, value)
REFRESH    = 0x0204479C           # redraw current page
PAGE_PARAM = 0x00800005           # FUNCTION page-index parameter
TITLE_DRAW = 0x0203C86C           # (x, y, title_bitmap_index)
LABELS_DRAW= 0x0203C254           # (record_list)
ONOFF_DRAW = 0x0203C434           # (x, y, onoff_byte)
CLOCK_SEND = 0x02041438           # transmit one MIDI clock tick burst
ROM_LIST   = 0x13D9D8             # file offset of the five page records
ROM_DESC   = 0x13DA3C             # file offset of the FUNCTION group descriptor
QUICK_CMP  = 0x24D72              # file offset of `cmp r3,#3` quick-key check
CLOCK_BL   = 0x215C8              # file offset of `bl CLOCK_SEND`
OUR_PAGE   = 5                    # new page index
OUR_KEY_ID = 0x17                 # quick-access key id that selects it
MIDI_ICON  = 2                    # title icon index 2 == the MIDI page icon

def be16(v): return struct.pack(">H", v)
def be32(v): return struct.pack(">I", v)

# ------------------------------------------------------------- thumb asm ----
# Minimal big-endian Thumb-1 encoder: emit with symbolic branch targets and a
# trailing 4-aligned literal pool, then resolve fixups in one closing pass.
class Asm:
    def __init__(self, org):
        self.org = org; self.buf = bytearray(); self.labels = {}
        self.fix = []       # (pos, kind, target)
        self.pool = []      # (index, value); positions resolved at end
        self.poolrefs = []  # (pos, reg, pool_index)
    def label(self, name):
        self.labels[name] = self.org + len(self.buf)
    def _h(self, hw): self.buf += be16(hw)
    def push(self, regs=(), lr=False):
        v = 0x0100 if lr else 0            # M bit (LR) is bit 8
        for r in regs: v |= 1 << r
        self._h(0xB400 | v)
    def pop(self, regs=(), pc=False):
        v = 0x0100 if pc else 0            # P bit (PC) is bit 8
        for r in regs: v |= 1 << r
        self._h(0xBC00 | v)
    def movs(self, rd, imm): self._h(0x2000 | (rd << 8) | imm)
    def ldr(self, rt, value):                 # literal pool load
        self.poolrefs.append((len(self.buf), rt, len(self.pool)))
        self.pool.append(value); self._h(0x4800 | (rt << 8))
    def ldrb(self, rt, rn, imm=0):
        assert 0 <= imm <= 31
        self._h(0x7800 | (imm << 6) | (rn << 3) | rt)
    def cmp(self, rd, imm): self._h(0x2800 | (rd << 8) | imm)
    def b(self, tgt, cond=None):
        kinds = {None: 0xE0, "eq": 0xD0, "ne": 0xD1, "hi": 0xD8, "lo": 0xD3,
                 "ge": 0xDA, "lt": 0xDB, "gt": 0xDC, "le": 0xDD}
        self.fix.append((len(self.buf), "b" if cond is None else "b"+cond, tgt))
        self._h(kinds[cond] << 8)
    def bl(self, abs_addr):
        self.fix.append((len(self.buf), "bl", abs_addr))
        self._h(0xF000); self._h(0xF800)
    def bx(self, rm): self._h(0x4700 | (rm << 3))
    def assemble(self):
        while len(self.buf) % 4:
            self._h(0x46C0)                   # nop
        pool_at = self.org + len(self.buf)
        for i, v in enumerate(self.pool):
            self.buf += be32(v)
        for pos, rt, idx in self.poolrefs:
            paddr = pool_at + 4 * idx
            pc = (self.org + pos + 4) & ~3
            assert (paddr - pc) % 4 == 0, "literal pool misaligned (org must be 4-aligned)"
            imm = (paddr - pc) // 4
            assert 0 <= imm <= 255, "literal out of range"
            self.buf[pos:pos+2] = be16(0x4800 | (rt << 8) | imm)
        for pos, kind, tgt in self.fix:
            at = self.org + pos
            if kind == "bl":
                off = tgt - (at + 4); assert off % 2 == 0, off
                off >>= 1
                self.buf[pos:pos+2] = be16(0xF000 | ((off >> 11) & 0x7FF))
                self.buf[pos+2:pos+4] = be16(0xF800 | (off & 0x7FF))
            else:
                t = self.labels[tgt] if isinstance(tgt, str) else tgt
                off = t - (at + 4); assert off % 2 == 0, off
                if kind == "b":
                    assert -2048 <= off < 2046, off
                    self.buf[pos:pos+2] = be16(0xE000 | ((off >> 1) & 0x7FF))
                else:
                    assert -254 <= off <= 254, (kind, off)
                    base = struct.unpack(">H", self.buf[pos:pos+2])[0] & 0xFF00
                    self.buf[pos:pos+2] = be16(base | ((off >> 1) & 0xFF))
        return bytes(self.buf)

def dis(addr, data, n=None):
    if md is None:
        return "  (capstone not installed; no listing)"
    out = []
    for i in md.disasm(data, addr):
        out.append("  %08x  %-8s %s" % (i.address, i.mnemonic, i.op_str))
    return "\n".join(out)

# ------------------------------------------------------------ clock label ---
FONT = {
 "C": [".###.","#...#","#....","#....","#....","#...#",".###."],
 "L": ["#....","#....","#....","#....","#....","#....","#####"],
 "O": [".###.","#...#","#...#","#...#","#...#","#...#",".###."],
 "K": ["#...#","#..#.","#.#..","##...","#.#..","#..#.","#...#"],
}
def make_clock_label():
    """32x52 1bpp label matching stock style: left rule + two text lines."""
    rows = [["."] * 32 for _ in range(52)]
    def text(s, y0, x0):
        for ci, ch in enumerate(s):
            g = FONT[ch]
            for gy, line in enumerate(g):
                for gx, c in enumerate(line):
                    if c == "#":
                        rows[y0 + gy][x0 + ci * 6 + gx] = "#"
    text("CLOC", 10, 2)
    text("K",    19, 2)
    for y in range(10, 32):                    # left rule like stock labels
        rows[y][0] = "#"
    out = bytearray()
    for y in range(52):
        bs = []
        for xb in range(4):
            b = 0
            for bit in range(8):
                b = (b << 1) | (1 if rows[y][xb * 8 + bit] == "#" else 0)
            bs.append(b)
        out += bytes(bs)
    return bytes(out)

# ------------------------------------------------------------------ main -----
def patch(img):
    d = bytearray(img)
    assert len(d) == 0x1E0000, "unexpected image size"

    # context assertions ---------------------------------------------------
    assert d[ROM_DESC:ROM_DESC+8] == bytes([5,0,0,0]) + be32(0x04011020), "descriptor mismatch"
    assert struct.unpack_from(">H", d, QUICK_CMP)[0] == 0x2B03, "quick-key cmp mismatch"
    bl = d[CLOCK_BL:CLOCK_BL+4]
    h1 = struct.unpack_from(">H", d, CLOCK_BL)[0]
    h2 = struct.unpack_from(">H", d, CLOCK_BL+2)[0]
    off = (((h1 & 0x7FF) << 12) | ((h2 & 0x7FF) << 1))
    if off & 0x400000: off -= 0x800000
    assert BASE + CLOCK_BL + 4 + off == CLOCK_SEND, "clock BL mismatch"
    assert all(b == 0xFF for b in d[0x140000:0x140400]), "free ROM not empty"
    # page 0's layout pointer is rewritten at FUNCTION entry (0x0204636C) into
    # RAM 0x04011024 by Part Mode (Voice Common 0x0D); our relocated list is
    # not touched by that write, so page 0 stays on the ROM (mono) layout.
    # See MENU_RE.md §3.1 and CHECKLIST Phase 3.
    for r in range(5):                          # the five page records
        o = ROM_LIST + r * 20
        assert d[o] in (1, 2, 3, 4) and d[o+1:o+4] == b"\0\0\0"

    # free-ROM allocator ----------------------------------------------------
    cur = [FREE]
    def alloc(blob, align=4, what=""):
        while (cur[0] - BASE) % align:
            img_at = cur[0] - BASE; d[img_at:img_at+1] = b"\x00"; cur[0] += 1
        at = cur[0]; cur[0] += len(blob)
        d[at - BASE:at - BASE + len(blob)] = blob
        assert cur[0] <= FREE_END, "free ROM overflow"
        return at

    # 1. CLOCK label bitmap + label list ------------------------------------
    bmp  = make_clock_label()
    BMPA = alloc(bmp, 4, "clock label bitmap")
    labellist = struct.pack(">hhhh", 0, 0x0C, 0x20, 0x34) + be32(BMPA | 0) \
              + bytes([0,0,0,0, 0,0,0,0, 0xFF,0,0,0, 0,0,0,0])   # terminator
    LBLA = alloc(labellist, 4, "label list")

    # 2. layout: one switch row on param 0x15 + three empty columns ---------
    rows  = bytes([0,0,CLOCK_FLAG,0, 4,0,0,0, 0,0,0,0, 0,0,0,0])
    rows += bytes([0,0x83,0x83,0, 0,0,0,0, 0,0,0,0, 0,0,0,0]) * 3
    LAYA = alloc(rows, 4, "layout")

    # 3..8 build code in free ROM.  Each function is assembled twice: once
    # with org 0 to learn its size (branch/literal encodings never change
    # size), the address is reserved, then it is re-assembled at its final
    # address so every internal reference is exact.
    def build_key(org):
        k = Asm(org)
        k.push([3], lr=True)
        k.cmp(0, 1);  k.b("ev1", "eq")
        k.cmp(0, 3);  k.b("ev3", "eq")
        k.movs(0, 0); k.b("out")
        k.label("ev1")
        # normalise flag: anything not 0/1 becomes 1 (on)
        k.ldr(3, SETTINGS); k.ldrb(3, 3, CLOCK_FLAG)
        k.cmp(3, 1);  k.b("norm", "hi")
        k.b("setpage")
        k.label("norm")
        k.ldr(0, 0x00000015); k.movs(1, 1); k.movs(2, 1)
        k.bl(SET_PARAM)
        k.label("setpage")
        k.ldr(0, PAGE_PARAM); k.movs(1, 1); k.movs(2, OUR_PAGE)
        k.bl(SET_PARAM)
        k.b("out")
        k.label("ev3")
        k.cmp(1, 8);  k.b("out0", "ne")
        k.ldrb(3, 2, 0); k.cmp(3, 2); k.b("out0", "ne")
        k.ldrb(3, 2, 1); k.cmp(3, OUR_KEY_ID); k.b("out0", "ne")
        k.ldr(3, SETTINGS); k.ldrb(3, 3, CLOCK_FLAG)
        k.movs(2, 1); k.cmp(3, 0); k.b("go", "eq")   # flag 0 (off) -> set 1
        k.movs(2, 0)                                  # else -> set 0
        k.label("go")
        k.ldr(0, 0x00000015); k.movs(1, 1)
        k.bl(SET_PARAM)
        k.bl(REFRESH)
        k.label("out0"); k.movs(0, 0)
        k.label("out")
        k.pop([3]); k.pop([1]); k.bx(1)
        return k.assemble()

    def build_static(org, label_list):
        s = Asm(org)
        s.push([3], lr=True)
        s.movs(0, 0); s.movs(1, 0); s.movs(2, MIDI_ICON)
        s.bl(TITLE_DRAW)
        s.ldr(0, label_list)
        s.bl(LABELS_DRAW)
        s.pop([3]); s.pop([1]); s.bx(1)
        return s.assemble()

    def build_value(org):
        v = Asm(org)
        v.push([3, 4], lr=True)
        v.ldr(3, SETTINGS); v.ldrb(4, 3, CLOCK_FLAG)
        v.movs(2, 1); v.cmp(4, 0); v.b("draw", "ne")
        v.movs(2, 0)
        v.label("draw")
        v.movs(0, 0); v.movs(1, 0x26)
        v.bl(ONOFF_DRAW)
        v.pop([3, 4]); v.pop([1]); v.bx(1)
        return v.assemble()

    def build_trampoline(org):
        t = Asm(org)
        t.ldr(3, SETTINGS); t.ldrb(3, 3, CLOCK_FLAG)
        t.cmp(3, 0); t.b("nosend", "eq")
        t.push(lr=True)
        t.bl(CLOCK_SEND)
        t.pop([3]); t.bx(3)
        t.label("nosend"); t.bx(14)
        return t.assemble()

    def reserve(blob_len, what=""):
        # code blobs end in a 4-aligned literal pool, so they must start 4-aligned
        while cur[0] % 4:
            d[cur[0] - BASE] = 0x00; cur[0] += 1
        at = cur[0]; cur[0] += blob_len
        assert cur[0] <= FREE_END, "free ROM overflow"
        return at

    def place(at, blob, what=""):
        d[at - BASE:at - BASE + len(blob)] = blob
        return at

    # probe sizes, reserve addresses, rebuild at final addresses
    key_p  = build_key(0)
    KEYA   = reserve(len(key_p))
    sta_p  = build_static(0, LBLA)
    STAA   = reserve(len(sta_p))
    val_p  = build_value(0)
    VALA   = reserve(len(val_p))
    tram_p = build_trampoline(0)
    TRAMPA = reserve(len(tram_p))

    keycode = build_key(KEYA);      assert len(keycode) == len(key_p)
    stcode  = build_static(STAA, LBLA); assert len(stcode) == len(sta_p)
    vcode   = build_value(VALA);    assert len(vcode) == len(val_p)
    tcode   = build_trampoline(TRAMPA); assert len(tcode) == len(tram_p)
    place(KEYA, keycode); place(STAA, stcode); place(VALA, vcode); place(TRAMPA, tcode)

    # 6. page record ----------------------------------------------------------
    record = bytes([1, 0, 0, 0]) + be32(LAYA) + be32(KEYA | 1) + be32(STAA | 1) + be32(VALA | 1)
    # 7. new page list: five stock records + ours
    newlist = bytes(d[ROM_LIST:ROM_LIST + 100]) + record
    LISTA = alloc(newlist, 4, "page list")

    # 9. in-place patches ------------------------------------------------------
    d[ROM_DESC:ROM_DESC+8] = bytes([6, 0, 0, 0]) + be32(LISTA)
    d[QUICK_CMP:QUICK_CMP+2] = be16(0x2B05)          # cmp r3,#5
    # rewrite bl at CLOCK_BL to trampoline
    off = TRAMPA - (BASE + CLOCK_BL + 4)
    assert off % 2 == 0
    o = off >> 1
    d[CLOCK_BL:CLOCK_BL+2]   = be16(0xF000 | ((o >> 11) & 0x7FF))
    d[CLOCK_BL+2:CLOCK_BL+4] = be16(0xF800 | (o & 0x7FF))

    report = []
    report.append("free-ROM placements:")
    for nm, a_ in (("clock label bitmap", BMPA), ("label list", LBLA),
                   ("layout", LAYA), ("key handler", KEYA), ("static draw", STAA),
                   ("value draw", VALA), ("page list", LISTA), ("trampoline", TRAMPA)):
        report.append("  %-20s %#08x (file %#x)" % (nm, a_, a_ - BASE))
    report.append("in-place patches:")
    report.append("  descriptor @ %#x: {6, %#x}" % (ROM_DESC, LISTA))
    report.append("  quick-key cmp @ %#x: #3 -> #5" % QUICK_CMP)
    report.append("  clock bl @ %#x -> trampoline" % CLOCK_BL)
    # verification disassembly
    report.append("\nkey handler:\n" + dis(KEYA, keycode))
    report.append("\nstatic draw:\n" + dis(STAA, stcode))
    report.append("\nvalue draw:\n" + dis(VALA, vcode))
    report.append("\ntrampoline:\n" + dis(TRAMPA, tcode))
    report.append("\npatched BL at 0x020415c8:\n" + dis(BASE + CLOCK_BL, bytes(d[CLOCK_BL:CLOCK_BL+4])))
    return bytes(d), "\n".join(report)

def main(argv):
    if len(argv) < 3:
        print(__doc__); return 2
    img = open(argv[1], "rb").read()
    out, report = patch(img)
    open(argv[2], "wb").write(out)
    print(report)
    if len(argv) > 3:
        open(argv[3], "wb").write(refacefw.repack(out))
        print("\nwrote updater payload", argv[3])
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
