#!/usr/bin/env python3
"""Behavioural verification of the MIDI 2 page patch by emulation (unicorn).

Applies patch_clock_menu.patch() to the stock image in memory, loads the
result at 0x02020000 under unicorn (big-endian Thumb), maps the 0x04xxxxxx RAM
window, and drives the new code the way the firmware would.  Stock routines
are used as control cases so a harness mistake cannot pass silently.

Checks (see MENU_RE.md section 8):
  static draw   calls title draw (0,0,2) and label draw (our list)
  label list    walked by the REAL stock routine 0x0203C254: exactly one blit,
                bitmap == CLOCK bitmap, rect x=0 y=0x0C 32x52; the pre-fix
                layout (bitmap at +8) is shown to blit from address 0
  trampoline    flag 0 -> clock sender not called; 1/2/0xFF -> called, r0 kept
  value draw    flag 0 -> OFF; nonzero -> ON, at (0, 0x26)
  key handler   event 1: page param set to 5, flag normalised first if invalid
                event 3: key id 0x17 toggles the flag + refresh; others ignored
  page apply    boot-copied descriptor lands at RAM 0x04011084 with 6 pages;
                0x020448CC with page 5 selects our record; page 2 on the stock
                image selects the stock MIDI record (control)

Usage: verify_menu.py [stock_image.bin]      (needs reface_csdx_fw/venv: unicorn, capstone)
"""
import sys, os, re, struct
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import patch_clock_menu as pm
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_BIG_ENDIAN, UC_HOOK_CODE
from unicorn.arm_const import *

BASE, IMG_SIZE = pm.BASE, 0x1E0000
RAM_BASE, RAM_SIZE = 0x04000000, 0x00200000       # covers 0x0401xxxx and 0x0414xxxx
STACK = 0x041F0000
RET   = 0x0FFFF000                                 # sentinel return address

# stock landmarks used by the tests
M_STATE      = 0x0411216C      # master UI state (MENU_RE section 5)
S_STATE      = 0x0414815C      # UI/0x0080-group buffer; +5 = current page
STOCK_MIDI_STATIC = 0x02047D68
STOCK_MIDI_LIST   = 0x020C4B30 + 0x20
STOCK_MIDI_BMPS   = [0x020AC0F8, 0x020AB418, 0x020AC300]
LCD_READY    = 0x020294A8      # draw helpers call this first; nonzero = go
BLIT         = 0x020291E8      # (rect*, bitmap)
PAGE_APPLY   = 0x020448CC
EVT_DISPATCH = 0x02045168      # called by page-apply when the record has a key handler
APPLY_DRAW   = 0x02045A2C
KEY_GUIDE    = 0x02044848
COPY_SRC, COPY_DST, COPY_END = 0x0215C9B8, 0x04010000, 0x040116E8   # boot copy (MENU_RE section 1)

R = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3]

class Emu:
    def __init__(self, img):
        uc = self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_BIG_ENDIAN)
        uc.mem_map(BASE, IMG_SIZE); uc.mem_write(BASE, img)
        uc.mem_map(RAM_BASE, RAM_SIZE)
        uc.mem_map(RET, 0x1000)
        self.calls, self.stubs, self.traces = [], {}, {}
        uc.hook_add(UC_HOOK_CODE, self._hook)

    def stub(self, addr, name, ret=0, capture=None):
        """Replace the function at addr with `bx lr`, log its arguments when hit."""
        self.uc.mem_write(addr, b"\x47\x70")
        self.stubs[addr] = (name, ret, capture)

    def trace(self, addr, name):
        """Log entry to addr without altering it."""
        self.traces[addr] = name

    def _hook(self, uc, addr, size, _):
        if addr in self.stubs:
            name, ret, cap = self.stubs[addr]
            args = tuple(uc.reg_read(r) for r in R)
            self.calls.append((name, args, cap(uc) if cap else None))
            uc.reg_write(UC_ARM_REG_R0, ret)
        elif addr in self.traces:
            self.calls.append((self.traces[addr], tuple(uc.reg_read(r) for r in R), None))

    def call(self, addr, *args, max_insns=50000, must_return=True):
        """Run addr(*args) until it returns to the sentinel.  Returns r0, or
        None when must_return is False and the instruction budget ran out."""
        for r, v in zip(R, args):
            self.uc.reg_write(r, v)
        self.uc.reg_write(UC_ARM_REG_SP, STACK)
        self.uc.reg_write(UC_ARM_REG_LR, RET | 1)
        self.calls = []
        self.uc.emu_start(addr | 1, RET, count=max_insns)
        pc = self.uc.reg_read(UC_ARM_REG_PC)
        if pc != RET:
            assert not must_return, "did not return cleanly (pc=%#x)" % pc
            return None
        return self.uc.reg_read(UC_ARM_REG_R0)

    def w8(self, addr, v):  self.uc.mem_write(addr, bytes([v]))
    def w32(self, addr, v): self.uc.mem_write(addr, struct.pack(">I", v))
    def r32(self, addr):    return struct.unpack(">I", self.uc.mem_read(addr, 4))[0]
    def named(self, name):  return [c for c in self.calls if c[0] == name]

def rect_capture(uc):
    return struct.unpack(">4I", uc.mem_read(uc.reg_read(UC_ARM_REG_R1), 16))

def placements(report):
    out = {}
    for m in re.finditer(r"^\s+(.+?)\s+(0x[0-9a-f]+) \(file", report, re.M):
        out[m.group(1).strip()] = int(m.group(2), 16)
    return out

# ----------------------------------------------------------------- tests ----
def check(cond, msg):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    return bool(cond)

def test_static_draw(img, P):
    print("static draw")
    ok = True
    e = Emu(img)
    e.stub(pm.TITLE_DRAW, "title"); e.stub(pm.LABELS_DRAW, "labels")
    e.w8(S_STATE + 5, 2)
    e.call(STOCK_MIDI_STATIC)
    ok &= check([c[1][:3] for c in e.named("title")] == [(0, 0, 2)]
                and [c[1][0] for c in e.named("labels")] == [STOCK_MIDI_LIST],
                "control: stock MIDI static draw -> title(0,0,2), labels(stock list)")
    e = Emu(img)
    e.stub(pm.TITLE_DRAW, "title"); e.stub(pm.LABELS_DRAW, "labels")
    e.call(P["static draw"])
    ok &= check([c[1][:3] for c in e.named("title")] == [(0, 0, pm.MIDI_ICON)],
                "ours: title draw (0, 0, MIDI icon)")
    ok &= check([c[1][0] for c in e.named("labels")] == [P["label list"]],
                "ours: label draw with our list %#x" % P["label list"])
    return ok

def test_label_walk(img, P):
    print("label list walked by stock 0x0203C254")
    ok = True
    def run(list_addr):
        e = Emu(img)
        e.stub(LCD_READY, "ready", ret=1)
        e.stub(BLIT, "blit", capture=rect_capture)
        e.call(pm.LABELS_DRAW, list_addr)
        return [(c[1][0], c[2]) for c in e.named("blit")]
    blits = run(STOCK_MIDI_LIST)
    ok &= check([b for b, _ in blits] == STOCK_MIDI_BMPS and blits[0][1] == (0, 0xC, 0x3F, 0x3F),
                "control: stock MIDI list -> 3 blits, bitmaps %s" % ", ".join("%#x" % b for b, _ in blits))
    blits = run(P["label list"])
    ok &= check(len(blits) == 1, "ours: exactly one blit (%d)" % len(blits))
    ok &= check(blits and blits[0][0] == P["clock label bitmap"],
                "ours: bitmap pointer == CLOCK bitmap %#x" % P["clock label bitmap"])
    ok &= check(blits and blits[0][1] == (0, 0x0C, 31, 0x3F), "ours: rect x=0 y=0x0C 32x52 -> %s" % (blits[0][1],))
    # negative control: the pre-4e9eecb 28-byte list (bitmap at +8, 0xFF at
    # +20) followed by the layout bytes, exactly as it sat in free ROM.  The
    # stock walker must be seen to blit from address 0 and never terminate.
    bad = RAM_BASE + 0x1000
    e = Emu(img); e.stub(LCD_READY, "ready", ret=1); e.stub(BLIT, "blit", capture=rect_capture)
    old_list = struct.pack(">hhhh", 0, 0xC, 0x20, 0x34) + struct.pack(">I", P["clock label bitmap"]) \
             + bytes([0, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0, 0, 0, 0, 0, 0, 0])
    layout = bytes(img[P["layout"] - BASE:P["layout"] - BASE + 64])
    e.uc.mem_write(bad, old_list + layout + b"\0" * 0x200)
    rc = e.call(pm.LABELS_DRAW, bad, max_insns=5000, must_return=False)
    b = [c[1][0] for c in e.named("blit")]
    ok &= check(b[:1] == [0] and rc is None and len(b) > 10,
                "negative control: pre-fix list blits from address 0 and never terminates (%d blits in 5000 insns)" % len(b))
    return ok

def test_trampoline(img, P):
    print("trampoline")
    ok = True
    for flag, expect in ((0, False), (1, True), (2, True), (0xFF, True)):
        e = Emu(img); e.stub(pm.CLOCK_SEND, "send")
        e.w8(pm.SETTINGS + pm.CLOCK_FLAG, flag)
        e.call(P["trampoline"], 0x1234)
        sends = e.named("send")
        ok &= check((len(sends) == 1) == expect and all(c[1][0] == 0x1234 for c in sends),
                    "flag %#x -> sender %s%s" % (flag, "called" if sends else "not called",
                                                  ", r0 preserved" if sends else ""))
    return ok

def test_value_draw(img, P):
    print("value draw")
    ok = True
    for flag, want in ((0, 0), (1, 1), (0xFF, 1)):
        e = Emu(img); e.stub(pm.ONOFF_DRAW, "onoff")
        e.w8(pm.SETTINGS + pm.CLOCK_FLAG, flag)
        e.call(P["value draw"])
        got = [c[1][:3] for c in e.named("onoff")]
        ok &= check(got == [(0, 0x26, want)], "flag %#x -> onoff%s" % (flag, got))
    return ok

def test_key_handler(img, P):
    print("key handler")
    ok = True
    K = P["key handler"]
    def run(event, r1=0, data=None, flag=1):
        e = Emu(img); e.stub(pm.SET_PARAM, "set"); e.stub(pm.REFRESH, "refresh")
        e.w8(pm.SETTINGS + pm.CLOCK_FLAG, flag)
        ev = RAM_BASE + 0x2000
        if data: e.uc.mem_write(ev, bytes(data))
        e.call(K, event, r1, ev if data else 0)
        return [c[1][:3] for c in e.named("set")], len(e.named("refresh"))
    for flag, want in ((1, [(pm.PAGE_PARAM, 1, pm.OUR_PAGE)]), (0, [(pm.PAGE_PARAM, 1, pm.OUR_PAGE)]),
                       (0xFF, [(0x15, 1, 1), (pm.PAGE_PARAM, 1, pm.OUR_PAGE)])):
        sets, _ = run(1, flag=flag)
        ok &= check(sets == want, "event 1, flag %#x -> set%s" % (flag, sets))
    for flag, want in ((0, (0x15, 1, 1)), (1, (0x15, 1, 0)), (0xFF, (0x15, 1, 0))):
        sets, refr = run(3, 8, [2, pm.OUR_KEY_ID], flag=flag)
        ok &= check(sets == [want] and refr == 1, "event 3 key %#x press, flag %#x -> set%s + refresh" % (pm.OUR_KEY_ID, flag, sets))
    for desc, args in (("key 0x12", (3, 8, [2, 0x12])), ("release (byte 3)", (3, 8, [3, pm.OUR_KEY_ID])),
                       ("r1 != 8", (3, 2, [2, pm.OUR_KEY_ID])), ("event 0", (0,)), ("event 2", (2,))):
        sets, refr = run(*args)
        ok &= check(sets == [] and refr == 0, "%s -> ignored" % desc)
    return ok

def boot_copy(e):
    e.uc.mem_write(COPY_DST, bytes(e.uc.mem_read(COPY_SRC, COPY_END - COPY_DST)))

def test_page_apply(stock, img, P):
    print("page apply")
    ok = True
    def run(image, page):
        e = Emu(image); boot_copy(e)
        e.stub(EVT_DISPATCH, "dispatch"); e.stub(APPLY_DRAW, "draw"); e.stub(KEY_GUIDE, "guide")
        e.w8(M_STATE + 3, 0); e.w8(M_STATE + 6, 1)          # target slot 0, not yet applied
        e.w32(M_STATE + 0xC, pm.ROM_DESC - 0x13C9B8 + COPY_DST)  # context = RAM descriptor
        e.w8(M_STATE + 4, page)
        e.call(PAGE_APPLY)
        return e, e.r32(M_STATE + 0x14)
    e, rec = run(stock, 2)
    ok &= check(rec == 0x04011020 + 2 * 0x14 and e.r32(rec + 8) == 0x02046131,
                "control: stock image, page 2 -> RAM record %#x with the stock MIDI key handler" % rec)
    e = Emu(img); boot_copy(e)
    desc = e.uc.mem_read(0x04011084, 8)
    ok &= check(desc == bytes([6, 0, 0, 0]) + struct.pack(">I", P["page list"]),
                "boot copy: RAM 0x04011084 = {6, %#x}" % P["page list"])
    e, rec = run(img, 5)
    want = P["page list"] + 5 * 0x14
    ok &= check(rec == want, "page 5 -> our record %#x" % rec)
    ok &= check(e.r32(rec + 8) == P["key handler"] | 1 and len(e.named("dispatch")) == 1,
                "our record's key handler is ours and the event dispatcher ran")
    e, rec = run(img, 2)
    ok &= check(e.r32(rec + 8) == 0x02046131, "page 2 on patched image still reaches the stock MIDI key handler")
    return ok

def main(argv):
    path = argv[1] if len(argv) > 1 else os.path.join(HERE, "..", "out", "reface_csdx_main_v130.bin")
    stock = open(path, "rb").read()
    img, report = pm.patch(stock)
    P = placements(report)
    results = [f(img, P) for f in (test_static_draw, test_label_walk, test_trampoline, test_value_draw, test_key_handler)]
    results.append(test_page_apply(stock, img, P))
    print("\nALL OK" if all(results) else "\nFAILURES")
    return 0 if all(results) else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv))
