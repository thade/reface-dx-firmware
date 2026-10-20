#!/usr/bin/env python3
"""Behavioural verification of the MIDI 2 page patch by emulation (unicorn).

Applies patch_clock_menu.patch() to the stock image in memory, loads the
result at 0x02020000 under unicorn (big-endian Thumb), maps the 0x04xxxxxx RAM
window, and drives the new code the way the firmware would.  Stock routines
are used as control cases so a harness mistake cannot pass silently.

Checks (see MENU_RE.md section 8):
  static draw   blits our own "MIDI 2" title bitmap (128x12 at 0,0, after the
                LCD-ready check) and calls label draw (our list)
  label list    walked by the REAL stock routine 0x0203C254: exactly one blit,
                bitmap == CLOCK bitmap, rect x=0 y=0x0C 32x52; the pre-fix
                layout (bitmap at +8) is shown to blit from address 0
  trampoline    flag 1 -> clock sender not called; 0/2/0xFF -> called, r0 kept;
                also driven through the real caller 0x020415B8
  value draw    flag 1 -> OFF; anything else -> ON, at (0, 0x26)
  key handler   event 1: page param set to 5
                event 3: key id 0x12 toggles the flag through the setter AND
                stores the byte directly (the 0x0B store handler drops
                addresses > 0x13) + refresh; others ignored
  dispatcher    the master dispatcher 0x02044CB4 with a real key event routes
                id 0x12 to our handler on page 5 (control: stock MIDI page, 0x14)
  real setter   registrar + hook installer + the REAL 0x0204564C: on the patched
                image 0x15 accepts 1 and 0 and param 0x00800005 accepts 5; on
                the stock image both are clamped (the bug the data patches fix)
  page apply    boot-copied descriptor lands at RAM 0x04011084 with 6 pages and
                the stock RAM list; 0x020448CC with page 5 selects our ROM record
                through the record hook, pages 0-4 the stock RAM records
  Part Mode     0x0204636C still rewrites page 0's layout (poly/mono) and page
                apply sees it, on stock and patched alike
  page cycler   0x02044960 advances 4 -> 5 (our record) -> wraps to 0
  checksums     sampled trailer lanes and 8-bit byte-sum equal the stock image

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
DISPATCHER   = 0x02044CB4      # master key-event dispatcher (event, data)
PAGE_CYCLE   = 0x02044960      # FUNCTION page cycler, honours the descriptor count
CLOCK_CALLER = 0x020415B8      # the only caller of the clock sender
SYNC_GETTER  = 0x02040428      # 0 = internal clock
CLOCK_OTHER  = 0x02041518      # other call inside CLOCK_CALLER
REGISTRAR    = 0x020425F0      # registers parameter groups at boot (returns 1)
HOOK_INSTALL = 0x02045988      # installs the setter's post-store hooks
POST_MSG     = 0x02037A34      # (type, payload*) message post used by the SYSTEM store hook
RAM_DESC     = 0x04011084

R = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3]

class Emu:
    def __init__(self, img):
        uc = self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_BIG_ENDIAN)
        uc.mem_map(BASE, IMG_SIZE); uc.mem_write(BASE, img)
        uc.mem_map(RAM_BASE, RAM_SIZE)
        uc.mem_map(RET, 0x1000)
        for base, size in ((0x07000000, 0x100000), (0x01000000, 0x100000), (0x00000000, 0x10000)):
            uc.mem_map(base, size)                 # peripherals / low memory touched by the real setter
        self.calls, self.stubs, self.traces = [], {}, {}
        uc.hook_add(UC_HOOK_CODE, self._hook)

    def stub(self, addr, name, ret=0, capture=None):
        """Replace the function at addr with `bx lr`, log its arguments when hit."""
        self.uc.mem_write(addr, b"\x47\x70")
        self.stubs[addr] = (name, ret, capture)

    def trace(self, addr, name, capture=None):
        """Log entry to addr without altering it (capture(uc) -> extra, like stub)."""
        self.traces[addr] = (name, capture)

    def _hook(self, uc, addr, size, _):
        if addr in self.stubs:
            name, ret, cap = self.stubs[addr]
            args = tuple(uc.reg_read(r) for r in R)
            self.calls.append((name, args, cap(uc) if cap else None))
            uc.reg_write(UC_ARM_REG_R0, ret)
        elif addr in self.traces:
            name, cap = self.traces[addr]
            self.calls.append((name, tuple(uc.reg_read(r) for r in R), cap(uc) if cap else None))

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
    e.stub(pm.LCD_READY, "ready", ret=1); e.stub(BLIT, "blit", capture=rect_capture); e.stub(pm.LABELS_DRAW, "labels")
    e.call(P["static draw"])
    blits = [(c[1][0], c[2]) for c in e.named("blit")]
    ok &= check(blits == [(P["MIDI 2 title"], (0, 0, 127, 11))],
                "ours: blits our MIDI 2 title bitmap at (0,0) 128x12 -> %s" % blits)
    ok &= check([c[1][0] for c in e.named("labels")] == [P["label list"]],
                "ours: label draw with our list %#x" % P["label list"])
    e = Emu(img)
    e.stub(pm.LCD_READY, "ready", ret=0); e.stub(BLIT, "blit", capture=rect_capture); e.stub(pm.LABELS_DRAW, "labels")
    e.call(P["static draw"])
    ok &= check(e.named("blit") == [] and len(e.named("labels")) == 1, "ours: LCD not ready -> no title blit (stock convention), labels still called")
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
    for flag, expect in ((1, False), (0, True), (2, True), (0xFF, True)):
        e = Emu(img); e.stub(pm.CLOCK_SEND, "send")
        e.w8(pm.SETTINGS + pm.CLOCK_FLAG, flag)
        e.call(P["trampoline"], 0x1234)
        sends = e.named("send")
        ok &= check((len(sends) == 1) == expect and all(c[1][0] == 0x1234 for c in sends),
                    "flag %#x -> sender %s%s" % (flag, "called" if sends else "not called",
                                                  ", r0 preserved" if sends else ""))
    for flag, expect in ((1, False), (0, True)):
        e = Emu(img); e.stub(pm.CLOCK_SEND, "send"); e.stub(SYNC_GETTER, "sync", ret=0); e.stub(CLOCK_OTHER, "other")
        e.w8(pm.SETTINGS + pm.CLOCK_FLAG, flag)
        e.call(CLOCK_CALLER, 0x55)
        sends = e.named("send")
        ok &= check((len(sends) == 1) == expect and all(c[1][0] == 0x55 for c in sends) and len(e.named("other")) == 1,
                    "via real caller 0x020415B8, flag %#x -> sender %s, caller continues" % (flag, "called" if sends else "not called"))
    return ok

def test_value_draw(img, P):
    print("value draw")
    ok = True
    for flag, want in ((1, 0), (0, 1), (0xFF, 1)):
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
    stored = {}
    def run(event, r1=0, data=None, flag=1):
        e = Emu(img); e.stub(pm.SET_PARAM, "set"); e.stub(pm.REFRESH, "refresh")
        e.w8(pm.SETTINGS + pm.CLOCK_FLAG, flag)
        ev = RAM_BASE + 0x2000
        if data: e.uc.mem_write(ev, bytes(data))
        e.call(K, event, r1, ev if data else 0)
        stored["flag"] = e.uc.mem_read(pm.SETTINGS + pm.CLOCK_FLAG, 1)[0]
        return [c[1][:3] for c in e.named("set")], len(e.named("refresh"))
    for flag in (0, 1, 0xFF):
        sets, _ = run(1, flag=flag)
        ok &= check(sets == [(pm.PAGE_PARAM, 1, pm.OUR_PAGE)], "event 1, flag %#x -> set%s (no normalisation)" % (flag, sets))
    for flag, want in ((0, (0x15, 1, 1)), (1, (0x15, 1, 0)), (0xFF, (0x15, 1, 1))):
        sets, refr = run(3, 8, [2, pm.OUR_KEY_ID], flag=flag)
        ok &= check(sets == [want] and refr == 1 and stored["flag"] == want[2],
                    "event 3 key %#x press, flag %#x -> set%s + direct store (byte now %#x) + refresh" % (pm.OUR_KEY_ID, flag, sets, stored["flag"]))
    for desc, args in (("key 0x14 (column 2)", (3, 8, [2, 0x14])), ("key 0x17", (3, 8, [2, 0x17])),
                       ("release (byte 3)", (3, 8, [3, pm.OUR_KEY_ID])),
                       ("r1 != 8", (3, 2, [2, pm.OUR_KEY_ID])), ("event 0", (0,)), ("event 2", (2,))):
        sets, refr = run(*args)
        ok &= check(sets == [] and refr == 0 and stored["flag"] == 1, "%s -> ignored, byte untouched" % desc)
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
    ok &= check(desc == bytes([6, 0, 0, 0]) + struct.pack(">I", 0x04011020),
                "boot copy: RAM 0x04011084 = {6, stock RAM list 0x04011020}")
    e, rec = run(img, 5)
    want = P["page 5 record"]
    ok &= check(rec == want, "page 5 -> our ROM record %#x (via the record hook)" % rec)
    ok &= check(e.r32(rec + 8) == P["key handler"] | 1 and len(e.named("dispatch")) == 1,
                "our record's key handler is ours and the event dispatcher ran")
    e, rec = run(img, 2)
    ok &= check(rec == 0x04011020 + 2 * 0x14 and e.r32(rec + 8) == 0x02046131,
                "page 2 on patched image -> stock RAM record with the MIDI key handler")
    for pg in range(5):
        e, rec = run(img, pg)
        ok &= check(rec == 0x04011020 + pg * 0x14, "page %d -> stock RAM record %#x" % (pg, rec))
    return ok

PARTMODE_SWAP = 0x0204636C      # FUNCTION-entry bookkeeping: picks page 0's layout from Part Mode
VOICE_COMMON  = 0x041480C4      # voice-common shadow; +0xD = Part Mode (0 poly, 1/2 mono)
LAYOUT_POLY, LAYOUT_MONO = 0x020C4098, 0x020C40D8

def test_partmode_swap(stock, img, P):
    print("Part Mode layout swap still reaches the Voice page")
    ok = True
    for name, image in (("stock", stock), ("patched", img)):
        for mode, want in ((0, LAYOUT_POLY), (1, LAYOUT_MONO), (2, LAYOUT_MONO)):
            e = Emu(image); boot_copy(e)
            e.stub(KEY_GUIDE, "guide"); e.stub(EVT_DISPATCH, "dispatch"); e.stub(APPLY_DRAW, "draw")
            e.w8(VOICE_COMMON + 0xD, mode)
            e.call(PARTMODE_SWAP)                       # what FUNCTION entry does first
            e.w8(M_STATE + 3, 0); e.w8(M_STATE + 6, 1); e.w8(M_STATE + 4, 0)
            e.call(PAGE_APPLY)                          # then page 0 is applied
            rec = e.r32(M_STATE + 0x14); lay = e.r32(rec + 4)
            ok &= check(rec == 0x04011020 and lay == want,
                        "%s: Part Mode %d -> page 0 record %#x layout %#x (%s)" % (name, mode, rec, lay, "poly" if want == LAYOUT_POLY else "mono"))
    return ok


def apply_page(e, page):
    """Simulate FUNCTION entry: boot copy, context = RAM descriptor, then page-apply."""
    boot_copy(e)
    e.w8(M_STATE + 3, 0); e.w8(M_STATE + 6, 1)
    e.w32(M_STATE + 0xC, RAM_DESC)
    e.w8(M_STATE + 4, page)
    e.call(PAGE_APPLY)

def test_dispatcher(stock, img, P):
    print("master dispatcher 0x02044CB4 with real key events")
    ok = True
    def run(image, page, key_id):
        e = Emu(image)
        e.stub(APPLY_DRAW, "draw"); e.stub(KEY_GUIDE, "guide")
        apply_page(e, page)
        e.stub(pm.SET_PARAM, "set"); e.stub(pm.REFRESH, "refresh")
        ev = RAM_BASE + 0x2000; e.uc.mem_write(ev, bytes([2, key_id]))
        e.call(DISPATCHER, 8, ev)
        return [c[1][:3] for c in e.named("set")], e.r32(M_STATE + 0x14)
    sets, rec = run(stock, 2, 0x14)
    ok &= check(sets == [(0x0E, 1, 1)] and rec == 0x04011020 + 2 * 0x14,
                "control: stock MIDI page, key 0x14 -> set(0x0E,1,1) [MIDI control]; page unchanged")
    ours = P["page 5 record"]
    sets, rec = run(img, 5, pm.OUR_KEY_ID)
    ok &= check(sets == [(0x15, 1, 1)] and rec == ours, "ours: page 5, key 0x12 -> set(0x15,1,1); page unchanged")
    sets, rec = run(img, 5, 0x14)
    ok &= check(sets == [] and rec == ours, "ours: page 5, key 0x14 -> nothing set; page unchanged")
    sets, rec = run(img, 2, 0x14)
    ok &= check(sets == [(0x0E, 1, 1)], "patched image, MIDI page, key 0x14 still toggles MIDI control")
    return ok

def test_cycler(stock, img, P):
    print("page cycler 0x02044960")
    ok = True
    e = Emu(stock); e.stub(EVT_DISPATCH, "dispatch"); e.stub(APPLY_DRAW, "draw"); e.stub(KEY_GUIDE, "guide")
    apply_page(e, 4); e.call(PAGE_CYCLE)
    ok &= check(e.uc.mem_read(M_STATE + 7, 1)[0] == 0, "control: stock image, page 4 -> wraps to 0 (5 pages)")
    e = Emu(img); e.stub(EVT_DISPATCH, "dispatch"); e.stub(APPLY_DRAW, "draw"); e.stub(KEY_GUIDE, "guide")
    apply_page(e, 4)
    e.call(PAGE_CYCLE)
    ours = P["page 5 record"]
    ok &= check(e.uc.mem_read(M_STATE + 7, 1)[0] == 5 and e.r32(M_STATE + 0x14) == ours,
                "patched: page 4 -> 5, record is ours")
    e.call(PAGE_CYCLE)
    ok &= check(e.uc.mem_read(M_STATE + 7, 1)[0] == 0, "patched: page 5 -> wraps to 0")
    return ok

def payload_capture(uc):
    return bytes(uc.mem_read(uc.reg_read(UC_ARM_REG_R1), 8))

def test_real_setter(stock, img, P):
    print("real parameter setter 0x0204564C (registrar + hooks first)")
    ok = True
    def run(image, param, value, pre=0):
        e = Emu(image); boot_copy(e)
        e.stub(POST_MSG, "post", ret=1, capture=payload_capture)
        assert e.call(REGISTRAR, max_insns=2000000) == 1, "registrar failed"
        e.call(HOOK_INSTALL)
        where = pm.SETTINGS + param if param < 0x20 else S_STATE + (param & 0xFF)
        e.w8(where, pre)
        e.call(pm.SET_PARAM, param, 1, value, max_insns=400000)
        posts = [c[2] for c in e.named("post")]
        return posts, e.uc.mem_read(where, 1)[0]
    posts, _ = run(stock, 0x0E, 1)
    ok &= check(len(posts) == 1 and posts[0][:4] == bytes([0, 0, 0x0E, 1]),
                "control: stock set(0x0E,1,1) posts value 1 -> %s" % (posts[0].hex() if posts else None))
    posts, _ = run(stock, 0x15, 1)
    ok &= check(posts == [], "stock set(0x15,1,1) from 0: clamped to 0, nothing posted (the bug)")
    posts, _ = run(img, 0x15, 1)
    ok &= check(len(posts) == 1 and posts[0][:4] == bytes([0, 0, 0x15, 1]),
                "patched set(0x15,1,1) from 0 posts value 1 -> %s" % (posts[0].hex() if posts else None))
    posts, _ = run(img, 0x15, 0, pre=1)
    ok &= check(len(posts) == 1 and posts[0][:4] == bytes([0, 0, 0x15, 0]),
                "patched set(0x15,1,0) from 1 posts value 0 -> %s" % (posts[0].hex() if posts else None))
    posts, _ = run(img, 0x15, 2)
    ok &= check(len(posts) == 1 and posts[0][3] == 1, "patched set(0x15,1,2) clamps to max 1")
    _, got = run(stock, pm.PAGE_PARAM, 5, pre=2)
    ok &= check(got == 4, "stock set(page,5) stores %d (clamped to 4: the bug)" % got)
    _, got = run(img, pm.PAGE_PARAM, 5, pre=2)
    ok &= check(got == 5, "patched set(page,5) stores %d" % got)
    _, got = run(img, pm.PAGE_PARAM, 2, pre=5)
    ok &= check(got == 2, "patched set(page,2) stores %d" % got)
    return ok

def test_checksums(stock, img, P):
    print("trailer checksums")
    import refacefw
    ok = True
    ok &= check(refacefw.sampled_lanes(stock) == refacefw.trailer_lanes(stock),
                "stock: sampled lanes == trailer bytes %s" % refacefw.trailer_lanes(stock).hex())
    ok &= check(refacefw.sampled_lanes(img) == refacefw.trailer_lanes(img) == refacefw.sampled_lanes(stock),
                "patched: sampled lanes unchanged %s" % refacefw.sampled_lanes(img).hex())
    ok &= check(refacefw.byte_sum(img) == refacefw.byte_sum(stock),
                "patched: 8-bit byte-sum unchanged %#04x" % refacefw.byte_sum(img))
    ok &= check(bytes(img[0x140000:0x140004]) == b"\xff" * 4, "file 0x140000..3 (sampled) untouched")
    return ok

def main(argv):
    path = argv[1] if len(argv) > 1 else os.path.join(HERE, "..", "out", "reface_csdx_main_v130.bin")
    stock = open(path, "rb").read()
    img, report = pm.patch(stock)
    P = placements(report)
    results = [f(img, P) for f in (test_static_draw, test_label_walk, test_trampoline, test_value_draw, test_key_handler)]
    results.append(test_page_apply(stock, img, P))
    for f in (test_partmode_swap, test_dispatcher, test_cycler, test_real_setter, test_checksums):
        results.append(f(stock, img, P))
    print("\nALL OK" if all(results) else "\nFAILURES")
    return 0 if all(results) else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv))
