#!/usr/bin/env python3
"""Baseline / verification probe for the reface DX over USB MIDI.

Sends a Universal Identity Request and listens for a few seconds, then reports:
the Identity Reply (model + firmware version), and counts of real-time bytes
seen (F8 clock, FE active sensing, FA/FB/FC) so clock behaviour is recorded
before and after flashing.

Usage: midi_probe.py [seconds] [port-substring]
"""
import sys, time, collections, mido

def main(argv):
    secs = float(argv[1]) if len(argv) > 1 else 4.0
    want = argv[2] if len(argv) > 2 else "reface"
    ins = [n for n in mido.get_input_names() if want.lower() in n.lower()]
    outs = [n for n in mido.get_output_names() if want.lower() in n.lower()]
    if not ins or not outs:
        print("no port matching %r. in=%s out=%s" % (want, mido.get_input_names(), mido.get_output_names()))
        return 1
    print("in :", ins[0]); print("out:", outs[0])
    counts = collections.Counter(); ident = None; others = []
    with mido.open_input(ins[0]) as i, mido.open_output(outs[0]) as o:
        o.send(mido.Message("sysex", data=[0x7E, 0x7F, 0x06, 0x01]))
        t0 = time.time(); clocks = []
        while time.time() - t0 < secs:
            for m in i.iter_pending():
                if m.type == "clock":
                    counts["F8 clock"] += 1; clocks.append(time.time())
                elif m.type == "active_sensing":
                    counts["FE active sensing"] += 1
                elif m.type in ("start", "continue", "stop"):
                    counts[m.type] += 1
                elif m.type == "sysex" and m.data[:1] == (0x7E,):      # universal (identity reply)
                    ident = bytes(m.data)
                else:
                    others.append(m)
            time.sleep(0.005)
    print("listened %.1fs:" % secs, dict(counts) or "no real-time bytes")
    if len(clocks) > 2:
        per = (clocks[-1] - clocks[0]) / (len(clocks) - 1)
        print("clock interval %.1f ms -> %.1f BPM" % (per * 1000, 60 / (per * 24)))
    if ident:
        d = ident
        print("identity reply:", d.hex())
        if len(d) >= 14 and d[3] == 0x02 and d[4] == 0x43:
            print("  Yamaha, family %02x%02x member %02x%02x, version bytes %s" % (d[5], d[6], d[7], d[8], d[9:13].hex()))
    else:
        print("no identity reply")
    if others:
        print("other messages:", others[:5])
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
