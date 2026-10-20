#!/usr/bin/env python3
"""UI simulator: feed real panel events through the firmware's own dispatcher.

Builds on verify_menu.Emu but maps memory lazily (any unmapped access gets a
zero page) so stock UI code can run largely unstubbed.  Only the LCD blit,
the MIDI byte transmitter, the OS event/interrupt/sleep helpers and the
flash driver are stubbed.

Parameter stores: the setter posts a type-0x0B message to the MIDI task's
ring buffer (0x04091994) and the store happens when that task processes it.
With receiver="real" (default) the post runs for real and, after every UI
event, the ring is drained through the firmware's own pop (0x02036CA0),
pre-dispatch (0x02036A6C) and per-type dispatch code (table 0x0209F1CC,
run with SP pointing at the popped message exactly as the task loop does).
receiver="manual" keeps the old shortcut: the post is stubbed and SYSTEM
stores are applied by hand, i.e. it models the UI side only and assumes the
store lands.  The same drain delivers SysEx (type 3 messages) to the real
parser 0x02037820 via UISim.sysex().

    sim = UISim(image)            # boot copy + registrar + hooks
    sim.key(0x0F)                 # press+release of micom key id 0x0F
    sim.state()                   # dict of the interesting UI state
    sim.lcd.text()                # what the screen shows
    sim.sysex(bytes)              # deliver a SysEx message to the MIDI task

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
# MIDI task (the consumer of the message ring that the parameter setter posts to)
RING       = 0x04091994        # ring header {write idx, read idx}, 16-byte entries from +8: type at +0, payload at +4
RING_POP   = 0x02036CA0        # (dst) -> 1 if a 16-byte message was copied out
PRE_DISPATCH = 0x02036A6C      # (msg) bookkeeping done before the per-type jump
DISPATCH_TABLE = 0x0209F1CC    # 16 code addresses inside the task loop, indexed by message type
LOOP_HEAD  = 0x02038348        # the task loop re-converges here after every message
OS_EVENT_SET = 0x02038418      # (bit, handle) wakes the task after a post
MIDI_STATE = 0x04095994        # MIDI task state; +0x25 = model flag (1 = DX, 0 = CS) from 0x020365D0 at task init
MODEL_FLAG = MIDI_STATE + 0x25
MIDI_TX    = 0x02036DB0        # (bytes, len) hands a MIDI message to the transmit driver
LOGGER     = 0x02020750        # assert/fatal logger: formats a message, then spins at 0x020207B4
VOICE_LOAD = 0x02039A24        # (slot) pushes a voice into the tone generator through its parameter table at
                               # 0x040915B0, which only the engine init fills; stubbed (voice select is out of scope)
TASK_SEND  = 0x0204D8A0        # OS send (port, msg) to another task's port; with no other task registered the
                               # callers retry forever with sleep, so it is stubbed to succeed and logged in .sent
MSG_BUF    = 0x04007000        # popped message lives here; the dispatch code runs with SP == MSG_BUF
SYSEX_BUF  = 0x04008000        # incoming SysEx bytes are written here before being posted as a type-3 message

def lazy(e):
    """Map memory on demand (zero pages) and fill low memory with `bx lr` so a
    call through a still-null function pointer (uninitialised RAM callback)
    returns harmlessly.  Returns the set that collects the touched pages."""
    unmapped = set()
    def hook(uc, access, addr, size, value, _):
        page = addr & ~0xFFF
        unmapped.add(page)
        uc.mem_map(page, 0x1000)
        return True
    e.uc.hook_add(UC_HOOK_MEM_READ_UNMAPPED | UC_HOOK_MEM_WRITE_UNMAPPED, hook)
    e.uc.mem_write(0, b"\x47\x70" * 0x8000)
    return unmapped

class UISim:
    def __init__(self, img, verbose=False, receiver="real"):
        self.img = img; self.verbose = verbose; self.receiver = receiver
        e = self.e = vm.Emu(img)
        self.lcd = lcd.LCD(); self.posts = []; self.handled = []; self.tx = []; self.fatal = []; self.frames = 0
        self.unmapped = lazy(e)
        e.stub(vm.LCD_READY, "ready", ret=1)
        e.stub(vm.BLIT, "blit", capture=self._blit)
        e.stub(FRAME_BEGIN, "frame_begin", capture=self._clear)
        e.stub(FRAME_END, "frame_end")
        if receiver == "manual":
            e.stub(vm.POST_MSG, "post", ret=1, capture=self._post_manual)
        else:
            e.trace(vm.POST_MSG, "post", capture=self._post)      # the real post writes the ring
            e.stub(OS_EVENT_SET, "os_event_set")
            e.uc.mem_write(MODEL_FLAG, b"\x01")                   # DX; set by the MIDI task init we do not run
        e.stub(MIDI_TX, "midi_tx", capture=self._tx)
        self.sent = []
        e.stub(VOICE_LOAD, "voice_load")
        e.stub(TASK_SEND, "task_send", capture=lambda uc: self.sent.append((uc.reg_read(UC_ARM_REG_R0), bytes(uc.mem_read(uc.reg_read(UC_ARM_REG_R1), 16)))))
        e.trace(LOGGER, "fatal", capture=self._fatal)
        for a, n in ((IRQ_OFF, "irq_off"), (IRQ_ON, "irq_on"), (SLEEP, "sleep"), (LAMP, "lamp"), (OS_WAIT, "os_wait"),
                     (LOOPER_Q1, "looper_q1"), (LOOPER_Q2, "looper_q2"), (FLASH_WRITE, "flash_write")):
            e.stub(a, n)
        vm.boot_copy(e)
        assert e.call(vm.REGISTRAR, max_insns=2000000) == 1
        e.call(vm.HOOK_INSTALL)
        self.log = []; self.hung = None

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
        return (t, payload)

    def _post_manual(self, uc):
        t, payload = self._post(uc)
        # receiver="manual": SYSTEM-group stores travel as type-0x0B messages
        # {addr(3), new, ...}; apply the store by hand (the real receiver only
        # does this for addresses 0x00-0x13, see verify_ui.test_receiver)
        if t == 0x0B and len(payload) >= 4 and payload[0] == 0 and payload[1] == 0:
            uc.mem_write(pm.SETTINGS + payload[2], bytes([payload[3]]))
        return (t, payload)

    def _tx(self, uc):
        p, n = uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1)
        data = bytes(uc.mem_read(p, n)) if p and 0 < n < 0x1000 else b""
        self.tx.append(data); return data

    def _fatal(self, uc):
        # 0x02020750(file, line, fmt, arg): record and stop the run before it spins
        f = (uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1))
        self.fatal.append(f); uc.emu_stop(); return f

    # ---- the MIDI task's message loop -------------------------------------
    def drain(self, max_insns=400000):
        """Process every message in the ring the way the MIDI task does:
        pop (0x02036CA0), pre-dispatch (0x02036A6C), then the per-type code
        from table 0x0209F1CC with SP == the popped message, until the loop
        head is reached again.  Returns the (type, payload) list handled."""
        if self.receiver != "real": return []
        e = self.e; done = []
        while e.call(RING_POP, MSG_BUF, max_insns=20000) == 1:
            msg = bytes(e.uc.mem_read(MSG_BUF, 16)); t = msg[0]
            e.call(PRE_DISPATCH, MSG_BUF, max_insns=20000)
            target = e.r32(DISPATCH_TABLE + 4 * t)
            e.calls = []
            e.uc.reg_write(UC_ARM_REG_SP, MSG_BUF)
            e.uc.emu_start(target | 1, LOOP_HEAD, count=max_insns)
            pc = e.uc.reg_read(UC_ARM_REG_PC)
            if pc != LOOP_HEAD and not self.fatal:
                self.hung = pc; print("   !! message type %#x %s did not return to the loop, pc=%#x" % (t, msg[4:].hex(), pc))
            done.append((t, msg[4:], [c[0] for c in e.calls]))
            self.handled.append(done[-1])
            if self.fatal: break
        return done

    def sysex(self, data):
        """Deliver one SysEx message: the receive driver hands the MIDI task a
        type-3 message {ptr, len}; the task feeds it to the parser 0x02037820."""
        e = self.e; data = bytes(data)
        e.uc.mem_write(SYSEX_BUF, data)
        e.uc.mem_write(SYSEX_BUF + 0x400, struct.pack(">II", SYSEX_BUF, len(data)))
        e.call(vm.POST_MSG, 3, SYSEX_BUF + 0x400, max_insns=20000)
        return self.drain()

    # ---- driving ---------------------------------------------------------
    def event(self, ev, data=None, max_insns=400000):
        buf = vm.RAM_BASE + 0x3000
        if data is not None: self.e.uc.mem_write(buf, bytes(data))
        r = self.e.call(UI_ENTRY, ev, buf if data is not None else 0, max_insns=max_insns, must_return=False)
        if r is None:
            self.hung = self.e.uc.reg_read(UC_ARM_REG_PC)
            print("   !! event %d %s did not return, pc=%#x" % (ev, data, self.hung))
        self.log.append((ev, bytes(data) if data else None, [c[0] for c in self.e.calls]))
        self.drain()
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
    sim.lcd = lcd.LCD(); sim.key(0x0F); print("EDIT (0x0F)  ", sim.fmt()); print("   ", sim.dump())
    print(sim.lcd.text()); print("blits:", sim.lcd.blits)
    for k in (0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A):
        sim.lcd = lcd.LCD(); sim.key(k); print("key %#04x     " % k, sim.fmt()); print("   ", sim.dump()); print("    blits:", sim.lcd.blits)
    sim.key(0x0F); print("EDIT (0x0F)  ", sim.fmt())
    print("unmapped pages touched:", ["%#x" % p for p in sorted(sim.unmapped)][:16])
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
