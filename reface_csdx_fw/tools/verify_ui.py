#!/usr/bin/env python3
"""UI-level verification of the MIDI 2 page patch: drive the firmware's own
top-level event entry (ui_sim.UISim) with real panel key events and check
what happens, with the stock image as the control.

  cycling       FUNCTION (micom key 0x0B) cycles pages 0-4 on stock, 0-5 on patched
  receiver      the posted type-0x0B store message is processed by the MIDI task's
                real message loop: stock key 0x14 lands byte 0x0E; patched key
                0x12 must land byte 0x15 (it does not: the receiver only
                handles system addresses 0x00-0x13 -- reported, not patched)
  toggle        column-0 key (0x12) on page 5 toggles byte 0x15 via the real setter
                (UI side: receiver="manual" applies the store by hand)
  cursor        other column keys on page 5 only move the cursor
  persistence   leaving FUNCTION copies the block to the NV shadow and writes flash
                only when something changed; re-entering returns to page 5 (manual store)
  screen        the deferred renderer draws page 5: MIDI icon, CLOCK label, ON/OFF (manual store)
  fuzz          random key sequences on stock and patched: no faults, identical
                state except on page 5 / byte 0x15, other settings bytes untouched

Usage: verify_ui.py [stock_image.bin] [--seed N] [--fuzz N]
"""
import sys, os, random
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import ui_sim, verify_menu as vm, patch_clock_menu as pm, lcd

FUNCTION, EDIT = 0x0B, 0x0F
COL = [0x12, 0x13, 0x14, 0x15]

def check(cond, msg):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg)); return bool(cond)

def fresh(img, receiver="real"):
    sim = ui_sim.UISim(img, receiver=receiver); sim.mode_switch(1); return sim

def goto_page(sim, page):
    for _ in range(8):
        if sim.state()["mode"] == 7 and sim.state()["applied_page"] == page: return
        sim.key(FUNCTION)
    raise AssertionError("page %d not reached" % page)

def test_cycling(stock, img, P):
    print("FUNCTION cycling (key 0x0B)")
    ok = True
    for name, image, n in (("stock", stock, 5), ("patched", img, 6)):
        sim = fresh(image); seq = []
        for _ in range(n + 2):
            sim.key(FUNCTION); st = sim.state(); seq.append((st["mode"], st["applied_page"]))
        want = [(7, i % n) for i in range(n + 2)]
        ok &= check(seq == want, "%s: %s" % (name, seq))
    sim = fresh(img); goto_page(sim, 5)
    ok &= check(sim.state()["record"] == P["page 5 record"], "patched page 5 record is ours (%#x)" % sim.state()["record"])
    return ok

def test_receiver(stock, img, P):
    print("store receiver: the MIDI task's real message loop processes the posted type-0x0B message")
    ok = True
    sim = fresh(stock); goto_page(sim, 2); sim.key(COL[2])
    h = [x for x in sim.handled if x[0] == 0x0B]
    ok &= check(len(h) == 1 and h[0][1][:4].hex() == "00000e01" and sim.e.uc.mem_read(pm.SETTINGS + 0x0E, 1)[0] == 1 and not sim.fatal,
                "control: stock MIDI page key 0x14 -> message %s popped and dispatched by the real loop, live byte 0x0E = %d"
                % (h[0][1][:4].hex() if h else None, sim.e.uc.mem_read(pm.SETTINGS + 0x0E, 1)[0]))
    sim = fresh(img); goto_page(sim, 5); sim.key(COL[0])
    h = [x for x in sim.handled if x[0] == 0x0B]; v = sim.e.uc.mem_read(pm.SETTINGS + 0x15, 1)[0]
    ok &= check(len(h) == 1 and h[0][1][:4].hex() == "00001501" and not sim.fatal and sim.hung is None,
                "patched page 5 key 0x12 -> message %s dispatched, no fatal, no hang" % (h[0][1][:4].hex() if h else None))
    ok &= check(v == 1, "patched: live byte 0x15 = %d after the real receiver ran (expected 1)%s" % (
        v, "" if v == 1 else "\n       -> the type-0x0B handler 0x02036C84 -> 0x0203A65C dispatches system addresses through"
        " table 0x0209FCBC only for lo <= 0x13 (cmp #0x13 at 0x0203A676); 0x15 falls through and is never stored."
        " On hardware the CLOCK toggle will do nothing visible. See docs/menu-internals.md section 2.3."))
    return ok

def test_toggle(stock, img, P):
    print("toggle on page 5 (UI side, receiver=manual: the store is applied by hand)")
    ok = True
    sim = fresh(img, "manual"); goto_page(sim, 5)
    seq = []
    for _ in range(3):
        sim.key(COL[0]); seq.append((sim.state()["flag15"], sim.posts[-1][1][:4].hex() if sim.posts else None))
    ok &= check(seq == [(1, "00001501"), (0, "00001500"), (1, "00001501")], "key 0x12 x3 -> flag/post %s" % seq)
    other = bytes(sim.e.uc.mem_read(pm.SETTINGS, 0x20)); other = other[:0x15] + other[0x16:]
    ok &= check(other == bytes(0x1F), "no other settings byte changed")
    sim = fresh(stock, "manual"); goto_page(sim, 2)        # control: MIDI page, key 0x14 toggles MIDI control (0x0E)
    sim.key(COL[2]); v = sim.e.uc.mem_read(pm.SETTINGS + 0x0E, 1)[0]
    ok &= check(v == 1 and sim.posts[-1][1][:4].hex() == "00000e01", "control: stock MIDI page key 0x14 -> MIDI control %d" % v)
    return ok

def test_cursor(stock, img, P):
    print("other column keys on page 5")
    ok = True
    sim = fresh(img); goto_page(sim, 5); n = len(sim.posts); f = sim.state()["flag15"]
    for k in COL[1:] + [0x17]:
        sim.key(k)
    st = sim.state()
    ok &= check(len(sim.posts) == n and st["flag15"] == f and st["applied_page"] == 5,
                "keys 0x13-0x15, 0x17 -> no parameter posts, flag unchanged, still page 5 (item=%d)" % st["item"])
    return ok

def test_persistence(stock, img, P):
    print("persistence on leaving FUNCTION (receiver=manual)")
    ok = True
    def writes(sim): return sum(1 for ev in sim.log for c in ev[2] if c == "flash_write")
    sim = fresh(stock, "manual"); sim.key(FUNCTION); sim.key(EDIT)
    ok &= check(writes(sim) == 0 and sim.state()["mode"] != 7, "control: stock, nothing changed -> leave FUNCTION, no flash write")
    sim = fresh(img, "manual"); goto_page(sim, 5); sim.key(COL[0])
    st = sim.state(); ok &= check(st["flag15"] == 1 and st["nv15"] == 0, "after toggle: live=1, NV shadow still 0")
    sim.key(EDIT); st = sim.state()
    ok &= check(st["mode"] != 7 and st["nv15"] == 1 and writes(sim) == 1, "leave FUNCTION: NV shadow byte 0x15 = 1, one flash write")
    sim.key(FUNCTION); st = sim.state()
    ok &= check(st["mode"] == 7 and st["applied_page"] == 5, "re-enter FUNCTION -> page 5 again (page index not clamped)")
    sim = fresh(stock, "manual"); goto_page(sim, 4); sim.key(EDIT); sim.key(FUNCTION)
    ok &= check(sim.state()["applied_page"] == 4, "control: stock re-enters on page 4")
    return ok

def test_screen(stock, img, P):
    print("screen (deferred renderer -> LCD model; receiver=manual)")
    ok = True
    sim = fresh(stock, "manual"); goto_page(sim, 2); sim.render(); l = sim.lcd
    ok &= check(len(l.blits) == 8 and l.column_ink(0) and l.column_ink(1) and l.column_ink(2) and l.column_ink(3),
                "control: stock MIDI page draws 8 blits, all four columns have content")
    frames = sim.frames; sim.key(FUNCTION); sim.render(); l = sim.lcd       # -> System page
    ok &= check(sim.frames > frames and all(b[0] in (0, 32, 64, 96) for b in l.blits),
                "control: page change begins a new frame (clears) before drawing (%d blits)" % len(l.blits))
    sim = fresh(img, "manual"); goto_page(sim, 4); sim.render()
    busy = [sim.lcd.column_ink(c) for c in range(4)]
    sim.key(FUNCTION); sim.render(); l = sim.lcd
    ok &= check(sim.state()["applied_page"] == 5 and l.blits[:3] == [(0, 0, 128, 12), (0, 12, 32, 52), (0, 38, 32, 32)],
                "page 5: title bar, label (0,12 32x52), value (0,38 32x32) -> %s" % l.blits)
    ok &= check(l.column_ink(1) == 0 and l.column_ink(2) == 0 and l.column_ink(3) == 0,
                "page 5: columns 1-3 empty after arriving from Job page (had ink %s)" % busy[1:])
    ok &= check(l.ink(0, 12, 31, 37) > 40, "page 5: CLOCK label has ink in column 0 (%d px)" % l.ink(0, 12, 31, 37))
    on_cell = l.ink(1, 38, 31, 63); nblits = len(l.blits); frames = sim.frames
    sim.key(COL[0]); sim.render(); l = sim.lcd
    off_cell = l.ink(1, 38, 31, 63)
    ok &= check(sim.frames == frames and l.blits[nblits:] == [(0, 38, 32, 32)] and off_cell != on_cell and off_cell > 0,
                "toggle redraws only the value cell, no frame clear: ON %d px -> off %d px" % (on_cell, off_cell))
    stock_sim = fresh(stock, "manual"); goto_page(stock_sim, 2); stock_sim.render()
    stock_off = stock_sim.lcd.ink(65, 38, 95, 63)   # CONTROL column, value off
    ok &= check(off_cell == stock_off, "our off graphic has the same ink as stock MIDI page's CONTROL off cell (%d px)" % stock_off)
    # page indicator: six dot cells at x = 82 + 8*i, rows 3..8; the filled one must be the current page
    def dots(l): return [l.ink(82 + 8 * i, 3, 87 + 8 * i, 8) for i in range(6)]
    sim = fresh(img); good = True; seen = {}
    for pg in range(6):
        goto_page(sim, pg); sim.render(); dk = dots(sim.lcd); seen[pg] = dk
        good &= all(v > 0 for v in dk) and dk.index(max(dk)) == pg
    ok &= check(good, "all six pages show six dots with the current page filled -> page 5 dots %s" % seen[5])
    ok &= check(sim.lcd.ink(71, 3, 75, 9) > 8, "page 5 title carries the 2 glyph")
    return ok

def test_fuzz(stock, img, P, seed, n):
    print("differential fuzz (%d sequences, seed %d)" % (n, seed))
    rnd = random.Random(seed); ok = True
    anykey = [FUNCTION, EDIT, 0x12, 0x13, 0x14, 0x15, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x00, 0x01, 0x02, 0x11]
    diverged = 0; faults = 0; reached5 = 0
    def gen():
        # random prefix, a burst of FUNCTION presses (most other keys leave
        # FUNCTION mode), some column keys, a random suffix
        return ([rnd.choice(anykey) for _ in range(rnd.randint(0, 3))] + [FUNCTION] * rnd.randint(1, 8)
                + [rnd.choice(COL) for _ in range(rnd.randint(0, 3))] + [rnd.choice(anykey) for _ in range(rnd.randint(0, 4))])
    for i in range(n):
        seq = gen()
        a, b = fresh(stock), fresh(img); same = True
        try:
            for k in seq:
                a.key(k); b.key(k)
                sa, sb = a.state(), b.state()
                if a.hung or b.hung: faults += 1; print("   fault in seq %s" % seq); break
                if sb["mode"] == 7 and sb["applied_page"] == 5: reached5 += 1
                if same and (sa["mode"], sa["applied_page"], sa["item"]) != (sb["mode"], sb["applied_page"], sb["item"]):
                    # stock wraps 4->0 where patched goes 4->5; from then on pages differ by design
                    same = False; diverged += 1
                    if not (sb["mode"] == 7 and sb["applied_page"] == 5):
                        faults += 1; print("   unexpected divergence %s vs %s after %s" % (sa, sb, seq))
            # once the page sequences have diverged the same key edits different
            # parameters (e.g. stock Tuning page vs patched Voice page), so the
            # settings comparison is only meaningful while they are in step
            sets_a = bytes(a.e.uc.mem_read(pm.SETTINGS, 0x20)); sets_b = bytes(b.e.uc.mem_read(pm.SETTINGS, 0x20))
            if same and sets_a[:0x15] + sets_a[0x16:] != sets_b[:0x15] + sets_b[0x16:]:
                faults += 1; print("   settings differ outside 0x15 after %s" % seq)
        except Exception as ex:
            faults += 1; print("   exception %s in seq %s" % (ex, seq))
    ok &= check(faults == 0 and reached5 > 0, "%d sequences, %d faults, %d diverged at the extra page (expected), page 5 visited %d times" % (n, faults, diverged, reached5))
    return ok

def main(argv):
    args = [a for i, a in enumerate(argv[1:], 1) if not a.startswith("--") and not argv[i - 1].startswith("--")]
    seed = int(argv[argv.index("--seed") + 1]) if "--seed" in argv else 1
    nfuzz = int(argv[argv.index("--fuzz") + 1]) if "--fuzz" in argv else 40
    path = args[0] if args else os.path.join(HERE, "..", "out", "reface_csdx_main_v130.bin")
    stock = open(path, "rb").read()
    img, _report, P = pm.patch(stock)
    results = [f(stock, img, P) for f in (test_cycling, test_receiver, test_toggle, test_cursor, test_persistence, test_screen)]
    results.append(test_fuzz(stock, img, P, seed, nfuzz))
    print("\nALL OK" if all(results) else "\nFAILURES")
    return 0 if all(results) else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv))
