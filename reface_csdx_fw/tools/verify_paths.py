#!/usr/bin/env python3
"""Emulation of the two paths that reach settings byte 0x15 without the UI:
the boot-time NV load and SysEx reception.  Stock image as control in every
case (see MENU_RE.md sections 2.3, 8 and 9).

  boot      an NV image (magic, SYSTEM shadow with byte 0x15 = 1, group
            0x784000 shadow) is placed at flash 0x02200000 and the real boot
            routine 0x02020E64 runs: flash -> RAM copy, magic check,
            shadow -> live restore, version fix-ups.  Asserts the live block
            ends with byte 0x15 = 1 and that the fix-ups only store 0x08 /
            0x11-0x13.  Invalid magic -> defaults -> byte 0x15 = 0.
  sysex     a Yamaha parameter change for system address 0x15 (values 0, 1,
            2), the control address 0x0E, a system bulk dump request and a
            system bulk dump block go through the MIDI task's real message
            loop and parser 0x02037820 (ui_sim.UISim.sysex).  Asserts no
            assert/fatal logger call and no hang, that the bulk dump carries
            byte 0x15, and (as specified by the task) that the patched image
            stores the clamped value for 0x15 -- which it does NOT: the
            receive paths only handle system addresses 0x00-0x13.

Usage: verify_paths.py [stock_image.bin]      (needs reface_csdx_fw/venv: unicorn, capstone)
"""
import sys, os, struct, io, contextlib
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import ui_sim, verify_menu as vm, patch_clock_menu as pm
from unicorn.arm_const import *

# ---- boot NV load (MENU_RE section 9) --------------------------------------
NV_FLASH, NV_RAM, NV_SIZE = 0x02200000, 0x04000000, 0x1800
MAGIC = 0xDEADFACE
NV_SYSTEM, NV_GROUP78, NV_VOICE = 0x1500, 0x1600, 0x100
BOOT_NV      = 0x02020E64      # load NV, restore live blocks, version fix-ups; returns a status
NV_LOAD      = 0x020443DC      # flash -> RAM copy + magic check; defaults + write when invalid
FLASH_INIT   = 0x020442E4      # boot-ROM flash driver query through the table at 0x04FF0000 (stubbed)
NV_COPY      = 0x02044244      # halfword copy flash 0x02200000 -> RAM 0x04000000, 0x1800 bytes (runs for real)
MAGIC_CHECK  = 0x02044318      # 0 when both magics match
DEFAULTS     = 0x02042A7C      # default-fill of the parameter blocks (byte 0x15 -> 0)
SHADOW_TO_LIVE = 0x0204281C    # 32-byte copy NV+0x1500 -> 0x041480A0
STORE        = 0x0204359C      # generic parameter store (addr, value)
FIXUPS       = {0x0204292C: "fixup_08", 0x02042944: "fixup_11_12", 0x02042974: "fixup_13"}
COMMIT       = 0x02044460      # (sel) live -> shadow, then flash write
MODE_BYTE    = 0x04091984 + 5  # system mode read by 0x02036220 (2 = update?, 3 = defaults just written)
FLAG6        = 0x04091984 + 6  # read by 0x02036238; 1 enables the 0x11/0x12 fix-up and the DX model flag

def check(cond, msg):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg)); return bool(cond)

def nv_image(system=bytes(32), group78=bytes(16), magic=True, trailing=True, fill=0xFF):
    nv = bytearray([fill]) * NV_SIZE
    nv[0:4] = struct.pack(">I", MAGIC if magic else 0x12345678)
    nv[NV_SIZE - 4:] = struct.pack(">I", MAGIC if trailing else 0)
    nv[NV_VOICE:NV_VOICE + 0x96] = bytes(0x96)
    nv[NV_SYSTEM:NV_SYSTEM + 32] = system
    nv[NV_GROUP78:NV_GROUP78 + 16] = group78
    return bytes(nv)

class Boot:
    def __init__(self, image, nv, flag6=0):
        e = self.e = vm.Emu(image); ui_sim.lazy(e)
        e.uc.mem_map(NV_FLASH, 0x2000); e.uc.mem_write(NV_FLASH, nv)
        self.writes = []; self.fatal = []
        e.stub(FLASH_INIT, "flash_init")
        e.stub(ui_sim.FLASH_WRITE, "flash_write", capture=lambda uc: self.writes.append(bytes(uc.mem_read(NV_RAM, NV_SIZE))))
        for a, n in ((ui_sim.IRQ_OFF, "irq_off"), (ui_sim.IRQ_ON, "irq_on"), (ui_sim.SLEEP, "sleep")):
            e.stub(a, n)
        e.trace(ui_sim.LOGGER, "fatal", capture=self._fatal)
        e.trace(STORE, "store"); e.trace(DEFAULTS, "defaults"); e.trace(SHADOW_TO_LIVE, "shadow_to_live")
        e.trace(COMMIT, "commit"); e.trace(NV_COPY, "nv_copy")
        for a, n in FIXUPS.items(): e.trace(a, n)
        e.uc.mem_write(FLAG6, bytes([flag6]))
        self.ret = e.call(BOOT_NV, max_insns=3000000, must_return=False)
        self.pc = e.uc.reg_read(UC_ARM_REG_PC)
        self.calls = list(e.calls)

    def _fatal(self, uc):
        f = (uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1)); self.fatal.append(f); uc.emu_stop(); return f

    def live(self):   return bytes(self.e.uc.mem_read(pm.SETTINGS, 32))
    def shadow(self): return bytes(self.e.uc.mem_read(NV_RAM + NV_SYSTEM, 32))
    def names(self):  return [c[0] for c in self.calls]
    def stores(self): return [c[1][0] for c in self.calls if c[0] == "store" and c[1][0] >> 8 == 0]   # SYSTEM group only

def test_boot(stock, img, P):
    print("boot NV load (0x02020E64 with the flash image at 0x02200000)")
    ok = True
    system = bytearray(32); system[0x0A] = 0x20; system[0x0E] = 1; system[0x15] = 1     # contrast, MIDI control, clock off
    nv = nv_image(bytes(system))
    for name, image in (("stock", stock), ("patched", img)):
        b = Boot(image, nv)
        ok &= check(b.ret is not None and not b.fatal and "nv_copy" in b.names() and "shadow_to_live" in b.names(),
                    "%s: boot returned (%s), copy + restore ran, no fatal logger call" % (name, b.ret))
        ok &= check(b.live()[0x15] == 1 and b.live()[0x0E] == 1 and b.live()[0x0A] == 0x20,
                    "%s: live block after boot: 0x15=%d 0x0E=%d 0x0A=%#x" % (name, b.live()[0x15], b.live()[0x0E], b.live()[0x0A]))
        fix = [n for n in b.names() if n.startswith("fixup")]
        ok &= check(set(fix) == set(FIXUPS.values()) and set(b.stores()) <= {0x08, 0x11, 0x12, 0x13} and 0x15 not in b.stores(),
                    "%s: version fix-ups %s store only SYSTEM %s" % (name, fix, sorted("%#x" % s for s in set(b.stores()))))
        diff = [i for i in range(32) if b.live()[i] != system[i]]
        ok &= check(all(i in (0x08, 0x09, 0x11, 0x12, 0x13) for i in diff),
                    "%s: live differs from the stored shadow only at fix-up bytes %s" % (name, ["%#x" % i for i in diff]))
    # flag 6 = 1 enables the 0x11/0x12 fix-up branch
    b = Boot(img, nv, flag6=1)
    ok &= check(not b.fatal and {0x11, 0x12} <= set(b.stores()) and b.live()[0x15] == 1,
                "patched, flag6=1: 0x11/0x12 fix-ups store too, 0x15 still 1 (stores %s)" % sorted("%#x" % s for s in set(b.stores())))
    # group 0x784000 byte 0 != 0 takes the other branch (no fix-ups)
    b = Boot(img, nv_image(bytes(system), group78=b"\x01" + bytes(15)))
    ok &= check(not b.fatal and b.ret is not None and not [n for n in b.names() if n.startswith("fixup")] and b.live()[0x15] == 1,
                "patched, group 0x784000 byte 0 = 1: no fix-ups, 0x15 = 1 (ret %s)" % b.ret)
    # invalid magic -> defaults
    for what, kw in (("leading magic", dict(magic=False)), ("trailing magic", dict(trailing=False))):
        for name, image in (("stock", stock), ("patched", img)):
            b = Boot(image, nv_image(bytes(system), **kw))
            ok &= check(not b.fatal and "defaults" in b.names() and len(b.writes) >= 1 and b.live()[0x15] == 0
                        and b.writes[0][NV_SYSTEM + 0x15] == 0 and struct.unpack(">I", b.writes[0][:4])[0] == MAGIC,
                        "%s, bad %s: defaults written (%d flash write), live 0x15=%d, written image 0x15=%d magic ok"
                        % (name, what, len(b.writes), b.live()[0x15], b.writes[0][NV_SYSTEM + 0x15] if b.writes else -1))
    return ok

# ---- SysEx reception (MENU_RE section 2.3) ---------------------------------
HDR = bytes([0xF0, 0x43])
def param_change(addr, value, dev=0):
    return HDR + bytes([0x10 | dev, 0x7F, 0x1C, 0x05]) + bytes(addr) + bytes([value, 0xF7])
def dump_request(addr, dev=0):
    return HDR + bytes([0x20 | dev, 0x7F, 0x1C, 0x05]) + bytes(addr) + bytes([0xF7])
def bulk_block(addr, data, dev=0):
    """F0 43 0n 7F 1C bc bc 05 a a a data.. cs F7; the firmware's 0x02036FF8
    sums bytes 7..(len+0xB) (model id, address, data, checksum) mod 128."""
    count = 4 + len(data); body = bytes([0x05]) + bytes(addr) + bytes(data)
    cs = (-sum(body)) & 0x7F
    return HDR + bytes([dev, 0x7F, 0x1C, count >> 7, count & 0x7F]) + body + bytes([cs, 0xF7])

def fresh(image):
    sim = ui_sim.UISim(image); sim.mode_switch(1); return sim

def clean(sim): return not sim.fatal and sim.hung is None

def test_sysex(stock, img, P):
    print("SysEx reception (type-3 message -> parser 0x02037820 -> DX handler 0x0203A810)")
    ok = True
    for name, image in (("stock", stock), ("patched", img)):
        sim = fresh(image); live = lambda a: sim.e.uc.mem_read(pm.SETTINGS + a, 1)[0]
        sim.sysex(param_change((0, 0, 0x0E), 1))
        ok &= check(clean(sim) and live(0x0E) == 1 and sim.handled and sim.handled[-1][0] == 3,
                    "%s: control: parameter change 0x0E=1 handled as a type-3 message, stored (0x0E=%d)" % (name, live(0x0E)))
        seq = []
        for v in (0, 1, 2):
            sim.sysex(param_change((0, 0, 0x15), v)); seq.append(live(0x15))
        want = [0, 1, 1] if name == "patched" else [0, 0, 0]
        ok &= check(clean(sim), "%s: parameter change 0x15 = 0/1/2: no fatal logger call, no hang" % name)
        ok &= check(seq == want, "%s: parameter change 0x15 = 0/1/2 -> live byte %s (expected %s%s)" % (
            name, seq, want, "" if seq == want else "; the DX handler 0x0203A8C8 only dispatches system addresses <= 0x13, 0x15 is ignored"))
        # bulk dump request: the whole 32-byte live block is transmitted
        sim.e.uc.mem_write(pm.SETTINGS + 0x15, b"\x01"); sim.tx = []
        sim.sysex(dump_request((0, 0, 0)))
        tx = sim.tx[-1] if sim.tx else b""
        body = tx[7:0x2C]; cs_ok = len(tx) == 0x2D and (sum(body) & 0x7F) == 0
        ok &= check(clean(sim) and tx[:11] == bytes([0xF0, 0x43, 0x00, 0x7F, 0x1C, 0x00, 0x24, 0x05, 0, 0, 0]) and tx[-1] == 0xF7 and cs_ok
                    and tx[11:11 + 32] == bytes(sim.e.uc.mem_read(pm.SETTINGS, 32)) and tx[11 + 0x15] == 1,
                    "%s: system dump request -> 45-byte bulk dump of the live block, checksum valid, byte 0x15 = %d transmitted"
                    % (name, tx[11 + 0x15] if len(tx) > 0x2C else -1))
        # bulk dump block for the system group: per-parameter receiver list
        sim.e.uc.mem_write(pm.SETTINGS + 0x0E, b"\x00"); sim.e.uc.mem_write(pm.SETTINGS + 0x15, b"\x00")
        data = bytearray(32); data[0x0A] = 0x20; data[0x0E] = 1; data[0x15] = 1
        sim.sysex(bulk_block((0, 0, 0), bytes(data)))
        ok &= check(clean(sim) and live(0x0E) == 1 and live(0x0A) == 0x20,
                    "%s: control: system bulk block stores 0x0E=%d 0x0A=%#x, no fatal" % (name, live(0x0E), live(0x0A)))
        got = live(0x15); want15 = 1 if name == "patched" else 0
        ok &= check(got == want15, "%s: system bulk block byte 0x15=1 -> live %d (expected %d%s)" % (
            name, got, want15, "" if got == want15 else "; 0x0203A214 stores bytes 0x00-0x0E, 0x11-0x13 one by one and skips 0x15"))
        # the "11 00 00" block commits the system block to flash (0x0203A10C -> 0x02044460(1))
        sim.e.uc.mem_write(pm.SETTINGS + 0x15, b"\x01")        # set by hand: shows the commit carries the byte
        writes = lambda: sum(1 for h in sim.handled for c in h[2] if c == "flash_write")
        n = writes(); sim.sysex(bulk_block((0x11, 0, 0), b""))
        nv15 = sim.e.uc.mem_read(ui_sim.NV_SHADOW + 0x15, 1)[0]
        ok &= check(clean(sim) and writes() == n + 1 and nv15 == 1,
                    "%s: bulk '11 00 00' block -> one flash write, NV shadow 0x15 = %d (live byte set by hand)" % (name, nv15))
        ok &= check(clean(sim) and sim.hung is None, "%s: no assert/fatal logger call anywhere in the sequence" % name)
    return ok

def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    path = args[0] if args else os.path.join(HERE, "..", "out", "reface_csdx_main_v130.bin")
    stock = open(path, "rb").read()
    with contextlib.redirect_stdout(io.StringIO()):
        img, report = pm.patch(stock)
    P = vm.placements(report)
    results = [f(stock, img, P) for f in (test_boot, test_sysex)]
    print("\nALL OK" if all(results) else "\nFAILURES")
    return 0 if all(results) else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv))
