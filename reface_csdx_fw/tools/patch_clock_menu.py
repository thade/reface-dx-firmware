#!/usr/bin/env python3
"""Add a "MIDI 2" FUNCTION page to the reface DX v1.30 image with a MIDI-clock switch.

Stock v1.30 has five FUNCTION pages (Voice, Tuning, MIDI, System, Job).  This
patch adds a sixth page, "MIDI 2", holding one on/off item labelled CLOCK that
controls whether MIDI Timing Clock (0xF8) is transmitted.  The flag lives in
the system settings block at SysEx-reserved address 0x15 and persists with the
normal settings save.  Semantics: the byte is a CLOCK-OFF flag -- 1 = clock
off, anything else (0 from the firmware's own default-fill, 0xFF from erased
flash) = clock on.  So the unit behaves exactly like stock until the user
turns the clock off, and no normalisation step is needed.

How the firmware works (verified against v1.30, see MENU_RE.md):
  * FUNCTION pages are 20-byte records {u8 columns, 3 pad, be32 layout,
    be32 key_handler, be32 static_draw, be32 value_draw} in shared RAM at
    0x04011020, copied at boot from ROM 0x13D9D8.  The group descriptor
    {u8 count, u8 current, u16 pad, be32 list} at ROM 0x13DA3C -> RAM
    0x04011084 selects them: page-apply 0x020448CC computes record = list +
    page*0x14 at 0x02044906..0x0204490C (the only such site).  The page
    cycler 0x02044960 honours the count byte, so a sixth page is reachable.
  * The stock list must stay in RAM: on every FUNCTION entry 0x0204636C
    rewrites record 0's layout pointer (RAM 0x04011024) from the voice's
    Part Mode (poly hides Portamento Time).  So instead of relocating the
    list we keep it, bump the count to 6, and hook the record computation:
    the two halfwords `lsls r3,r3,#2; adds r3,r2,r3` become `bl rechook`,
    which does the same arithmetic and substitutes our ROM record when the
    page is 5.
  * A page's layout is 16-byte rows {addr_hi, addr_mid, addr_lo, pad, type,...};
    row bytes 0..2 are the 24-bit SysEx parameter address, 0x83 marks an
    empty column.  Type 4 is an on/off switch.
  * Settings live at RAM 0x041480A0, byte offset == SysEx address.  Every
    parameter has a 20-byte entry {ptr, default, min, max, type}; the setter
    clamps to [min, max].  Stock entry 0x15 is all zero (min = max = 0), so it
    must be patched to max 1 before the flag can be set.  The page-index
    parameter 0x00800005 has max 4 and is raised to 5.
  * The setter does not store SYSTEM bytes itself: it posts a type-0x0B
    message that the MIDI task's handler 0x0203A65C applies -- but only for
    addresses <= 0x13.  Our key handler therefore stores byte 0x15 directly
    after calling the setter (the call is kept for the stock MIDI
    transmit-on-change behaviour).
  * Key events: master dispatcher 0x02044CB4 forwards event 3 to the page's
    key handler; micom key ids 0x12-0x15 are the four column keys (id - 0x12
    = column).  CLOCK is column 0, so our toggle key is 0x12.
  * The clock sender 0x02041438 is called from exactly one place,
    0x020415C8 inside 0x020415B8, when not following external clock.

Patch sites (file offsets):
  0x13DA3C  group descriptor count byte      5 -> 6 (list pointer unchanged)
  0x2490A   page-apply record arithmetic     -> bl rechook (page 5 -> our record)
  0x215C8   bl clock sender                  -> bl trampoline (checks the flag)
  0x97188   SYSTEM entry 0x15 {0,0,0,0,0}    -> {0,0,0,1,0}   (max 1)
  0x96BA4   UI entry 5 max                   4 -> 5           (page index)

New code/data is placed in the 0xFF erase-fill from file 0x140004 (address
0x02160004) onwards.  File 0x140000..3 is left untouched because the
firmware's trailer checksum samples it (refacefw.sampled_offsets); the patch
asserts the sampled lanes are unchanged and balances the 8-bit byte-sum with
one pad byte so both trailer checks still pass.  Everything is big-endian
Thumb-1.

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
FREE      = 0x02160004            # first free-ROM address we use (file 0x140004)
FREE_END  = 0x02160000 + 0x20000  # generous working limit (below the next sampled chunk)

# ---- stock firmware landmarks (asserted before patching) --------------------
SETTINGS   = 0x041480A0           # system settings block (byte == SysEx addr)
CLOCK_FLAG = 0x15                 # reserved on both CS and DX; 1 = clock OFF
SET_PARAM  = 0x0204564C           # setter(param_addr, op=1, value)
REFRESH    = 0x0204479C           # redraw current page
PAGE_PARAM = 0x00800005           # FUNCTION page-index parameter
TITLE_DRAW = 0x0203C86C           # (x, y, title_bitmap_index)
LABELS_DRAW= 0x0203C254           # (record_list)
ONOFF_DRAW = 0x0203C434           # (x, y, onoff_byte)
CLOCK_SEND = 0x02041438           # transmit one MIDI clock tick burst
ROM_LIST   = 0x13D9D8             # file offset of the five page records
ROM_DESC   = 0x13DA3C             # file offset of the FUNCTION group descriptor
CLOCK_BL   = 0x215C8              # file offset of `bl CLOCK_SEND`
REC_HOOK   = 0x2490A              # file offset of `lsls r3,r3,#2; adds r3,r2,r3` in page-apply
RAM_LIST   = 0x04011020           # the stock five records, boot-copied into RAM
SYS_ENTRIES= 0x96FE4              # file offset of the SYSTEM per-parameter entries (32 x 20 bytes)
UI_ENTRIES = 0x96B34              # file offset of the 0x0080 UI-group entries (44 x 20 bytes)
ENTRY_15   = SYS_ENTRIES + CLOCK_FLAG * 20   # 0x97188
UI_ENTRY_5 = UI_ENTRIES + 5 * 20             # 0x96B98
OUR_PAGE   = 5                    # new page index
OUR_KEY_ID = 0x12                 # column-0 key id (ids 0x12-0x15 = columns 0-3)
BLIT       = 0x020291E8           # (bitmap, rect{x0,y0,x1,y1}*) 1bpp blit into the framebuffer
LCD_READY  = 0x020294A8           # (0) -> nonzero when the LCD may be drawn
TITLE_TABLE= 0x020A494C           # +0x24 + 4*page -> 128x12 title bitmap (pages 0-4)
TITLE_W, TITLE_H = 128, 12
NPAGES     = 6

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
    def strb(self, rt, rn, imm=0):
        assert 0 <= imm <= 31
        self._h(0x7000 | (imm << 6) | (rn << 3) | rt)
    def cmp(self, rd, imm): self._h(0x2800 | (rd << 8) | imm)
    def lsls(self, rd, rm, imm):
        assert 0 < imm < 32
        self._h(0x0000 | (imm << 6) | (rm << 3) | rd)
    def add3(self, rd, rn, rm): self._h(0x1800 | (rm << 6) | (rn << 3) | rd)
    def sub_sp(self, imm): assert imm % 4 == 0 and imm < 512; self._h(0xB080 | (imm >> 2))
    def add_sp(self, imm): assert imm % 4 == 0 and imm < 512; self._h(0xB000 | (imm >> 2))
    def str_sp(self, rt, imm): assert imm % 4 == 0 and imm < 1024; self._h(0x9000 | (rt << 8) | (imm >> 2))
    def add_rd_sp(self, rd, imm=0): assert imm % 4 == 0 and imm < 1024; self._h(0xA800 | (rd << 8) | (imm >> 2))
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
    text("CLOCK", 10, 2)                       # 5 glyphs x 6 px pitch = x 2..31
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

# ----------------------------------------------------------- title bars ----
DOT_FILLED = ["..##..", ".####.", "######", "######", ".####.", "..##.."]
DOT_EMPTY  = ["..##..", ".#..#.", "#....#", "#....#", ".#..#.", "..##.."]
DOT_X0, DOT_PITCH, DOT_Y = 82, 8, 3          # six dots end at x = 127 like the stock five
GLYPH_2 = [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"]

def unpack_bitmap(data, w, h):
    stride = (w + 7) // 8
    return [[(data[y * stride + (x >> 3)] >> (7 - (x & 7))) & 1 for x in range(w)] for y in range(h)]

def pack_bitmap(rows, w):
    out = bytearray()
    for row in rows:
        for xb in range((w + 7) // 8):
            b = 0
            for bit in range(8):
                x = xb * 8 + bit
                b = (b << 1) | (row[x] if x < w else 0)
            out.append(b)
    return bytes(out)

def retitle(rows, page, npages=NPAGES, shift=6):
    """Rebuild a stock 128x12 title: keep the text (columns < 88) shifted
    `shift` px left, replace the five dots with `npages` dots, `page` filled."""
    out = [[0] * TITLE_W for _ in range(TITLE_H)]
    for y in range(TITLE_H):
        for x in range(88):
            if rows[y][x] and x - shift >= 0:
                out[y][x - shift] = 1
    for i in range(npages):
        g = DOT_FILLED if i == page else DOT_EMPTY
        for gy, line in enumerate(g):
            for gx, c in enumerate(line):
                if c == "#": out[DOT_Y + gy][DOT_X0 + i * DOT_PITCH + gx] = 1
    return out

def make_midi2_title(midi_rows):
    """"MIDI 2": the stock MIDI glyphs (columns 55..74) plus a 2 in the same font."""
    rows = [[0] * TITLE_W for _ in range(TITLE_H)]
    for y in range(TITLE_H):
        for x in range(55, 75):
            rows[y][x] = midi_rows[y][x]
    for gy, line in enumerate(GLYPH_2):
        for gx, c in enumerate(line):
            if c == "#": rows[3 + gy][77 + gx] = 1
    return retitle(rows, NPAGES - 1)

# ------------------------------------------------------------------ main -----
def patch(img):
    d = bytearray(img)
    assert len(d) == 0x1E0000, "unexpected image size"
    stock_lanes, stock_sum = refacefw.sampled_lanes(d), refacefw.byte_sum(d)
    assert stock_lanes == refacefw.trailer_lanes(d), "stock trailer lanes do not verify"

    # context assertions ---------------------------------------------------
    assert d[ROM_DESC:ROM_DESC+8] == bytes([5,0,0,0]) + be32(0x04011020), "descriptor mismatch"
    h1 = struct.unpack_from(">H", d, CLOCK_BL)[0]
    h2 = struct.unpack_from(">H", d, CLOCK_BL+2)[0]
    off = (((h1 & 0x7FF) << 12) | ((h2 & 0x7FF) << 1))
    if off & 0x400000: off -= 0x800000
    assert BASE + CLOCK_BL + 4 + off == CLOCK_SEND, "clock BL mismatch"
    assert d[REC_HOOK-4:REC_HOOK+6] == bytes.fromhex("0083181b009b18d36163"), "page-apply record arithmetic mismatch"
    assert all(b == 0xFF for b in d[0x140000:0x140400]), "free ROM not empty"
    for r in range(5):                          # the five page records
        o = ROM_LIST + r * 20
        assert d[o] in (1, 2, 3, 4) and d[o+1:o+4] == b"\0\0\0"
    # parameter entries: {be32 ptr, be32 default, be32 min, be32 max, be32 type}
    assert d[SYS_ENTRIES + 0x0E*20:SYS_ENTRIES + 0x0F*20] == be32(0)*3 + be32(1) + be32(0), "entry 0x0E mismatch"
    assert d[ENTRY_15:ENTRY_15+20] == bytes(20), "entry 0x15 not all-zero"
    assert d[UI_ENTRY_5:UI_ENTRY_5+20] == be32(0)*3 + be32(4) + be32(0), "UI entry 5 mismatch"

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
    # label record (16 bytes, as 0x0203C254 reads it): {s16 x, y, w, h;
    # be32 zero (byte 8 == 0xFF marks the terminator); be32 bitmap at +0xC}
    labellist = struct.pack(">hhhh", 0, 0x0C, 0x20, 0x34) + be32(0) + be32(BMPA) \
              + bytes([0,0,0,0, 0,0,0,0, 0xFF,0,0,0, 0,0,0,0])   # terminator
    assert len(labellist) == 32
    LBLA = alloc(labellist, 4, "label list")

    # 2. layout: one switch row on param 0x15 + three empty columns ---------
    rows  = bytes([0,0,CLOCK_FLAG,0, 4,0,0,0, 0,0,0,0, 0,0,0,0])
    rows += bytes([0,0x83,0x83,0, 0,0,0,0, 0,0,0,0, 0,0,0,0]) * 3
    LAYA = alloc(rows, 4, "layout")

    # 2b. title bars: six page dots everywhere, and a "MIDI 2" title of our own
    titles = [struct.unpack_from(">I", d, TITLE_TABLE - BASE + 0x24 + 4 * i)[0] for i in range(5)]
    stock_rows = [unpack_bitmap(d[t - BASE:t - BASE + TITLE_W * TITLE_H // 8], TITLE_W, TITLE_H) for t in titles]
    for i, t in enumerate(titles):
        assert d[t - BASE + 3 * 16 + 11] == 0x60 or sum(stock_rows[i][3][88:]) > 0, "title %d has no dots row" % i
        d[t - BASE:t - BASE + TITLE_W * TITLE_H // 8] = pack_bitmap(retitle(stock_rows[i], i), TITLE_W)
    TITLEA = alloc(pack_bitmap(make_midi2_title(stock_rows[2]), TITLE_W), 4, "MIDI 2 title")

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
        k.label("ev1")                                 # page became active
        k.ldr(0, PAGE_PARAM); k.movs(1, 1); k.movs(2, OUR_PAGE)
        k.bl(SET_PARAM)
        k.b("out0")
        k.label("ev3")                                 # key event {press, id}
        k.cmp(1, 8);  k.b("out0", "ne")
        k.ldrb(3, 2, 0); k.cmp(3, 2); k.b("out0", "ne")
        k.ldrb(3, 2, 1); k.cmp(3, OUR_KEY_ID); k.b("out0", "ne")
        k.ldr(3, SETTINGS); k.ldrb(3, 3, CLOCK_FLAG)
        k.movs(2, 1); k.cmp(3, 1); k.b("go", "ne")    # flag != 1 (clock on) -> set 1 (off)
        k.movs(2, 0)                                  # flag == 1 (off)      -> set 0 (on)
        k.label("go")
        k.push([2])                                   # keep the new value across the setter
        k.ldr(0, 0x00000000 | CLOCK_FLAG); k.movs(1, 1)
        k.bl(SET_PARAM)                               # stock path: clamp + post (MIDI tx on change)
        k.pop([2])
        # The 0x0B store handler 0x0203A65C only stores addresses <= 0x13, so
        # the posted value for 0x15 is dropped there: store the byte ourselves.
        k.ldr(3, SETTINGS); k.strb(2, 3, CLOCK_FLAG)
        k.bl(REFRESH)
        k.label("out0"); k.movs(0, 0)
        k.label("out")
        k.pop([3]); k.pop([1]); k.bx(1)
        return k.assemble()

    def build_static(org, label_list, title_bmp):
        # title: blit our 128x12 bitmap at (0,0) the way 0x0203C86C does,
        # rect {x0, y0, x1, y1} built on the stack; then the label list
        s = Asm(org)
        s.push([4], lr=True)
        s.sub_sp(16)
        s.movs(0, 0); s.bl(LCD_READY)
        s.cmp(0, 0); s.b("labels", "eq")
        s.movs(3, 0); s.str_sp(3, 0); s.str_sp(3, 4)
        s.movs(3, TITLE_W - 1); s.str_sp(3, 8)
        s.movs(3, TITLE_H - 1); s.str_sp(3, 12)
        s.ldr(0, title_bmp); s.add_rd_sp(1, 0)
        s.bl(BLIT)
        s.label("labels")
        s.ldr(0, label_list)
        s.bl(LABELS_DRAW)
        s.add_sp(16)
        s.pop([4]); s.pop([1]); s.bx(1)
        return s.assemble()

    def build_value(org):
        v = Asm(org)
        v.push([3, 4], lr=True)
        v.ldr(3, SETTINGS); v.ldrb(4, 3, CLOCK_FLAG)
        v.movs(2, 1); v.cmp(4, 1); v.b("draw", "ne")  # flag != 1 -> ON
        v.movs(2, 0)                                  # flag == 1 -> OFF
        v.label("draw")
        v.movs(0, 0); v.movs(1, 0x26)
        v.bl(ONOFF_DRAW)
        v.pop([3, 4]); v.pop([1]); v.bx(1)
        return v.assemble()

    def build_trampoline(org):
        t = Asm(org)
        t.ldr(3, SETTINGS); t.ldrb(3, 3, CLOCK_FLAG)
        t.cmp(3, 1); t.b("nosend", "eq")              # flag == 1 -> clock off
        t.push([4], lr=True)                          # keep the stack 8-aligned
        t.bl(CLOCK_SEND)
        t.pop([4]); t.pop([3]); t.bx(3)
        t.label("nosend"); t.bx(14)
        return t.assemble()

    def build_rechook(org, record):
        # entered from page-apply with r0 = page, r2 = list, r3 = page*5
        h = Asm(org)
        h.lsls(3, 3, 2)                               # r3 = page*20
        h.add3(3, 2, 3)                               # r3 = list + page*20 (stock result)
        h.cmp(0, OUR_PAGE); h.b("done", "ne")
        h.ldr(3, record)                              # page 5 -> our ROM record
        h.label("done"); h.bx(14)
        return h.assemble()

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
    sta_p  = build_static(0, LBLA, TITLEA)
    STAA   = reserve(len(sta_p))
    val_p  = build_value(0)
    VALA   = reserve(len(val_p))
    tram_p = build_trampoline(0)
    TRAMPA = reserve(len(tram_p))
    hook_p = build_rechook(0, 0)
    HOOKA  = reserve(len(hook_p))

    # 6. page record (ROM; the stock five stay in RAM) --------------------------
    record = bytes([1, 0, 0, 0]) + be32(LAYA) + be32(KEYA | 1) + be32(STAA | 1) + be32(VALA | 1)
    RECA = alloc(record, 4, "page 5 record")

    keycode = build_key(KEYA);      assert len(keycode) == len(key_p)
    stcode  = build_static(STAA, LBLA, TITLEA); assert len(stcode) == len(sta_p)
    vcode   = build_value(VALA);    assert len(vcode) == len(val_p)
    tcode   = build_trampoline(TRAMPA); assert len(tcode) == len(tram_p)
    hcode   = build_rechook(HOOKA, RECA); assert len(hcode) == len(hook_p)
    place(KEYA, keycode); place(STAA, stcode); place(VALA, vcode); place(TRAMPA, tcode); place(HOOKA, hcode)

    # 9. in-place patches ------------------------------------------------------
    def put_bl(file_off, target):
        off = target - (BASE + file_off + 4)
        assert off % 2 == 0 and -0x400000 <= off < 0x400000, "bl out of range"
        o = off >> 1
        d[file_off:file_off+2]   = be16(0xF000 | ((o >> 11) & 0x7FF))
        d[file_off+2:file_off+4] = be16(0xF800 | (o & 0x7FF))
    d[ROM_DESC] = 6                                   # six pages; list pointer stays RAM_LIST
    put_bl(CLOCK_BL, TRAMPA)
    put_bl(REC_HOOK, HOOKA)
    d[ENTRY_15:ENTRY_15+20]   = be32(0) + be32(0) + be32(0) + be32(1) + be32(0)   # max 1
    d[UI_ENTRY_5+12:UI_ENTRY_5+16] = be32(5)                                       # page max 5

    # 10. trailer checksums ----------------------------------------------------
    # The sampled lanes must be untouched (we start at 0x140004 for that);
    # balance the 8-bit byte-sum with one pad byte in free ROM.
    assert refacefw.sampled_lanes(d) == stock_lanes, "patch touched a sampled checksum offset"
    PADA = alloc(bytes([(stock_sum - refacefw.byte_sum(d) + 0xFF) & 0xFF]), 1, "byte-sum pad")   # pad replaces a 0xFF byte
    assert refacefw.byte_sum(d) == stock_sum, "byte-sum balance failed"

    report = []
    report.append("free-ROM placements:")
    for nm, a_ in (("clock label bitmap", BMPA), ("label list", LBLA),
                   ("layout", LAYA), ("MIDI 2 title", TITLEA), ("key handler", KEYA), ("static draw", STAA),
                   ("value draw", VALA), ("trampoline", TRAMPA), ("record hook", HOOKA),
                   ("page 5 record", RECA), ("byte-sum pad", PADA)):
        report.append("  %-20s %#08x (file %#x)" % (nm, a_, a_ - BASE))
    report.append("in-place patches:")
    report.append("  descriptor count @ %#x: 5 -> 6 (list stays %#x)" % (ROM_DESC, RAM_LIST))
    report.append("  record arithmetic @ %#x -> bl record hook" % REC_HOOK)
    report.append("  clock bl @ %#x -> trampoline" % CLOCK_BL)
    report.append("  SYSTEM entry 0x15 @ %#x: max 0 -> 1" % ENTRY_15)
    report.append("  UI entry 5 @ %#x: max 4 -> 5" % UI_ENTRY_5)
    report.append("  title bitmaps @ %s: five dots -> six (text shifted 6 px left)" % ", ".join("%#x" % t for t in titles))
    report.append("trailer: sampled lanes %s unchanged, byte-sum %#04x unchanged"
                  % (stock_lanes.hex(), stock_sum))
    report.append("\nkey handler:\n" + dis(KEYA, keycode))
    report.append("\nstatic draw:\n" + dis(STAA, stcode))
    report.append("\nvalue draw:\n" + dis(VALA, vcode))
    report.append("\ntrampoline:\n" + dis(TRAMPA, tcode))
    report.append("\nrecord hook:\n" + dis(HOOKA, hcode))
    report.append("\npatched page-apply at 0x02044906:\n" + dis(BASE + REC_HOOK - 4, bytes(d[REC_HOOK-4:REC_HOOK+6])))
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
