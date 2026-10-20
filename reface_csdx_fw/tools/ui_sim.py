#!/usr/bin/env python3
"""UI simulator: feed real panel events through the firmware's own dispatcher.

Builds on verify_menu.Emu but maps memory lazily (any unmapped access gets a
zero page) so stock UI code can run largely unstubbed.  Only the LCD blit,
the MIDI/engine message post and the interrupt/sleep helpers are stubbed.

    sim = UISim(image)            # boot copy + registrar + hooks
    sim.key(0x0F)                 # press+release of micom key id 0x0F
    sim.state()                   # dict of the interesting UI state
    sim.lcd.text()                # what the screen shows

Usage: ui_sim.py [stock_image.bin]   -- runs an exploratory scenario and prints state
"""
import sys, os, struct
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import verify_menu as vm, patch_clock_menu as pm, lcd
from unicorn import UC_HOOK_MEM_UNMAPPED, UC_HOOK_MEM_READ_UNMAPPED, UC_HOOK_MEM_WRITE_UNMAPPED, UC_HOOK_MEM_FETCH_UNMAPPED
from unicorn.arm_const import *

IRQ_OFF, IRQ_ON, SLEEP = 0x02020DF0, 0x02020E24, 0x02031124
LAMP = 0x02045AFC
LOOPER_Q1, LOOPER_Q2 = 0x02041058, 0x0204085C   # looper state queries made before entering FUNCTION (assert on uninitialised RAM)
FLASH_WRITE = 0x020897D8       # writes the 0x1800-byte NV image at RAM 0x04000000 to flash 0x02200000 via the boot-ROM driver
NV_SHADOW = 0x04001500         # SYSTEM block shadow inside the NV image (saved by 0x02042840 before the write)
RENDER_TICK = 0x0203BE20       # deferred LCD renderer (flag bits; 4 = redraw the current screen record)
FRAME_BEGIN = 0x020290AC       # LCD begin-frame: clears the framebuffer before a full redraw
FRAME_END   = 0x020290C8       # LCD end-frame: pushes the framebuffer to the panel
UI_ENTRY = 0x02044FD0          # top-level UI event entry (event, data): mode handler, then mode-switch keys
OS_WAIT = 0x02023760           # OS primitive (semaphore/flag wait) used by the mode switch

class UISim:
    def __init__(self, img, verbose=False):
        self.img = img; self.verbose = verbose
        e = self.e = vm.Emu(img)
        self.lcd = lcd.LCD(); self.posts = []; self.unmapped = set(); self.frames = 0
        e.uc.hook_add(UC_HOOK_MEM_READ_UNMAPPED | UC_HOOK_MEM_WRITE_UNMAPPED, self._unmapped)
        # low memory is filled with `bx lr` so a call through a still-null
        # function pointer (uninitialised RAM callback) returns harmlessly
        e.uc.mem_write(0, b"\x47\x70" * 0x8000)
        e.stub(vm.LCD_READY, "ready", ret=1)
        e.stub(vm.BLIT, "blit", capture=self._blit)
        e.stub(FRAME_BEGIN, "frame_begin", capture=self._clear)
        e.stub(FRAME_END, "frame_end")
        e.stub(vm.POST_MSG, "post", ret=1, capture=self._post)
        for a, n in ((IRQ_OFF, "irq_off"), (IRQ_ON, "irq_on"), (SLEEP, "sleep"), (LAMP, "lamp"), (OS_WAIT, "os_wait"),
                     (LOOPER_Q1, "looper_q1"), (LOOPER_Q2, "looper_q2"), (FLASH_WRITE, "flash_write")):
            e.stub(a, n)
        vm.boot_copy(e)
        assert e.call(vm.REGISTRAR, max_insns=2000000) == 1
        e.call(vm.HOOK_INSTALL)
        self.log = []; self.hung = None

    def _unmapped(self, uc, access, addr, size, value, _):
        page = addr & ~0xFFF
        self.unmapped.add(page)
        uc.mem_map(page, 0x1000)
        return True

    def _clear(self, uc):
        self.lcd = lcd.LCD(); self.frames += 1

    def _blit(self, uc):
        bmp = uc.reg_read(UC_ARM_REG_R0); rect = struct.unpack(">4I", uc.mem_read(uc.reg_read(UC_ARM_REG_R1), 16))
        x0, y0, x1, y1 = rect; w, h = x1 - x0 + 1, y1 - y0 + 1
        n = ((w + 7) // 8) * h
        data = bytes(uc.mem_read(bmp, n)) if 0 < n < 0x4000 and bmp else b""
        self.lcd.blit(data, rect)
        return (bmp, rect)

    def _post(self, uc):
        t = uc.reg_read(UC_ARM_REG_R0); p = uc.reg_read(UC_ARM_REG_R1)
        payload = bytes(uc.mem_read(p, 12)) if p else b""
        self.posts.append((t, payload))
        # SYSTEM-group stores travel as type-0x0B messages {addr(3), new, ...}
        # and are applied by the receiver; emulate that store here so the
        # settings block reflects the change.
        if t == 0x0B and len(payload) >= 4 and payload[0] == 0 and payload[1] == 0:
            uc.mem_write(pm.SETTINGS + payload[2], bytes([payload[3]]))
        return (t, payload)

    # ---- driving ---------------------------------------------------------
    def event(self, ev, data=None, max_insns=400000):
        buf = vm.RAM_BASE + 0x3000
        if data is not None: self.e.uc.mem_write(buf, bytes(data))
        r = self.e.call(UI_ENTRY, ev, buf if data is not None else 0, max_insns=max_insns, must_return=False)
        if r is None:
            self.hung = self.e.uc.reg_read(UC_ARM_REG_PC)
            print("   !! event %d %s did not return, pc=%#x" % (ev, data, self.hung))
        self.log.append((ev, bytes(data) if data else None, [c[0] for c in self.e.calls]))
        return r

    def key(self, key_id, release=True):
        self.event(8, [2, key_id])
        if release: self.event(8, [3, key_id])

    def mode_switch(self, mode, key=0x0F):
        return self.e.call(0x02044DB8, mode, key, max_insns=400000)

    def render(self):
        """Run the deferred renderer: it calls the current record's static and
        value draw routines, whose blits land in self.lcd."""
        for flags in (4, 0x10):
            self.e.call(RENDER_TICK, flags, max_insns=400000, must_return=False)
        return self.lcd

    # ---- observing -------------------------------------------------------
    def state(self):
        e = self.e; M = vm.M_STATE; S = vm.S_STATE
        m = bytes(e.uc.mem_read(M, 0x1C))
        return {"mode": m[0], "mode1": m[1], "page": m[4], "item": m[5], "applied_page": m[7], "applied_item": m[8],
                "record": e.r32(M + 0x14), "S_page": e.uc.mem_read(S + 5, 1)[0],
                "desc_cur": e.uc.mem_read(vm.RAM_DESC + 1, 1)[0], "flag15": e.uc.mem_read(pm.SETTINGS + 0x15, 1)[0],
                "nv15": e.uc.mem_read(NV_SHADOW + 0x15, 1)[0]}

    def dump(self):
        e = self.e
        return "M=%s S=%s" % (bytes(e.uc.mem_read(vm.M_STATE, 0x20)).hex(" ", 4), bytes(e.uc.mem_read(vm.S_STATE, 0x10)).hex(" ", 4))

    def fmt(self):
        s = self.state()
        return "mode=%d/%d page=%d item=%d applied=%d/%d rec=%#x S5=%d desc.cur=%d flag15=%#x nv15=%#x" % (
            s["mode"], s["mode1"], s["page"], s["item"], s["applied_page"], s["applied_item"], s["record"],
            s["S_page"], s["desc_cur"], s["flag15"], s["nv15"])

def main(argv):
    path = argv[1] if len(argv) > 1 else os.path.join(HERE, "..", "out", "reface_csdx_main_v130.bin")
    stock = open(path, "rb").read()
    sim = UISim(stock)
    sim.mode_switch(1); print("mode 1       ", sim.fmt()); print("   ", sim.dump())
    sim.lcd = lcd.LCD(); sim.key(0x0F); print("FUNCTION     ", sim.fmt()); print("   ", sim.dump())
    print(sim.lcd.text()); print("blits:", sim.lcd.blits)
    for k in (0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A):
        sim.lcd = lcd.LCD(); sim.key(k); print("key %#04x     " % k, sim.fmt()); print("   ", sim.dump()); print("    blits:", sim.lcd.blits)
    sim.key(0x0F); print("FUNCTION     ", sim.fmt())
    print("unmapped pages touched:", ["%#x" % p for p in sorted(sim.unmapped)][:16])
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
