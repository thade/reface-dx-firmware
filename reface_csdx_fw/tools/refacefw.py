#!/usr/bin/env python3
"""Yamaha reface CS/DX firmware updater payload: extract and repack.

The official updater ships its firmware as a Standard MIDI File (format 0)
containing Yamaha bulk-dump SysEx messages.  Each message is

    F0 43 00 7F 1C <cnt hi> <cnt lo> 03 50 00 <data...> <cs> F7

where cnt is a 14-bit count covering address (3 bytes) + data, and cs is
(-(sum(address + data))) & 0x7F.

Message 0 carries a 9-byte header.  Messages 1..N carry a 5-byte sub-header
    01 <blk hi> <blk lo> <len hi> <len lo>
followed by 7-bit packed data: groups of 8 bytes, first byte holds the MSBs
(bit 6 -> first data byte ... bit 0 -> seventh).  The length field counts raw
bytes + number of groups (i.e. packed bytes before padding to a full group).

Usage:
    refacefw.py extract  <updater.dat|.mid>  <image.bin>
    refacefw.py repack   <image.bin>  <out.mid>
    refacefw.py roundtrip <original.mid>          (extract, repack, compare)
"""
import sys, struct

YAMAHA_HDR = bytes([0x43, 0x00, 0x7F, 0x1C])
ADDRESS = bytes([0x03, 0x50, 0x00])
RAW_PER_BLOCK = 14322          # raw bytes per full block (2046 groups of 7)
HEADER_MSG_DATA = bytes.fromhex("00010a000010080000")
BLOCK_DELTA = 1309             # SMF delta-time before each data block
TIMESIG = bytes.fromhex("04021808")
TEMPO = bytes.fromhex("07a120")

# ---------------------------------------------------------------- SMF parsing
def _varlen_read(d, p):
    v = 0
    while True:
        b = d[p]; p += 1
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, p

def _varlen(v):
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append(0x80 | (v & 0x7F)); v >>= 7
    return bytes(reversed(out))

def parse_smf(d):
    """Return list of (delta, body) for every SysEx message. body excludes F0.

    A message split across an F0 event and following F7 continuation events
    (the SMF way of chunking long SysEx) is reassembled into one body.
    """
    assert d[:4] == b"MThd", "not a Standard MIDI File"
    ntracks = struct.unpack(">H", d[10:12])[0]
    assert ntracks == 1, "expected a single-track (format 0) file, got %d tracks" % ntracks
    p = 14
    assert d[p:p + 4] == b"MTrk"
    tl = struct.unpack(">I", d[p + 4:p + 8])[0]; p += 8; end = p + tl
    msgs = []; running = None; open_sysex = False
    while p < end:
        dt, p = _varlen_read(d, p)
        st = d[p]
        if st == 0xF0:
            p += 1; ln, p = _varlen_read(d, p)
            body = d[p:p + ln]; p += ln
            msgs.append((dt, bytearray(body)))
            open_sysex = body[-1:] != b"\xf7"
        elif st == 0xF7:
            p += 1; ln, p = _varlen_read(d, p)
            body = d[p:p + ln]; p += ln
            if open_sysex:                      # continuation of the previous F0 event
                msgs[-1][1].extend(body)
                open_sysex = body[-1:] != b"\xf7"
            # otherwise an "escape" event (raw bytes); not SysEx, ignore
        elif st == 0xFF:
            p += 2; ln, p = _varlen_read(d, p); p += ln
        else:
            if st & 0x80:
                running = st; p += 1
            if running is None:
                raise ValueError("data byte at %#x with no running status" % p)
            hi = running >> 4
            if hi in (0xC, 0xD):
                p += 1
            elif hi == 0xF:                     # system common: F1,F3 = 1 data byte, F2 = 2, F6 = 0
                p += {0xF1: 1, 0xF2: 2, 0xF3: 1}.get(running, 0)
            else:
                p += 2
    return [(dt, bytes(b)) for dt, b in msgs]

# ------------------------------------------------------------- SysEx helpers
def yamaha_checksum(addr_and_data):
    return (-sum(addr_and_data)) & 0x7F

def split_msg(body):
    """body = bytes after F0 up to and including F7. Verifies framing and checksum, returns data."""
    assert body[:4] == YAMAHA_HDR, body[:8].hex()
    cnt = (body[4] << 7) | body[5]
    assert body[6:9] == ADDRESS
    data = body[9:-2]; cs = body[-2]
    assert body[-1] == 0xF7
    assert cnt == 3 + len(data), (cnt, len(data))
    assert yamaha_checksum(body[6:-2]) == cs, "checksum mismatch"
    return data

def build_msg(data):
    cnt = 3 + len(data)
    core = bytes([cnt >> 7, cnt & 0x7F]) + ADDRESS + data
    return YAMAHA_HDR + core + bytes([yamaha_checksum(ADDRESS + data), 0xF7])

# -------------------------------------------------------------- 7-bit packing
def unpack7(p):
    out = bytearray()
    assert len(p) % 8 == 0
    for i in range(0, len(p), 8):
        msb = p[i]
        for j in range(7):
            out.append(p[i + 1 + j] | (0x80 if msb & (1 << (6 - j)) else 0))
    return bytes(out)

def pack7(raw):
    out = bytearray()
    for i in range(0, len(raw), 7):
        grp = raw[i:i + 7] + b"\x00" * (7 - len(raw[i:i + 7]))
        msb = 0
        for j, b in enumerate(grp):
            if b & 0x80:
                msb |= 1 << (6 - j)
        out.append(msb); out += bytes(b & 0x7F for b in grp)
    return bytes(out)

# ------------------------------------------------------------------- extract
def extract(d):
    msgs = parse_smf(d)
    hdr = split_msg(msgs[0][1])
    img = bytearray()
    for i, (dt, body) in enumerate(msgs[1:]):
        data = split_msg(body)
        sub, packed = data[:5], data[5:]
        blk = (sub[1] << 7) | sub[2]; ln = (sub[3] << 7) | sub[4]
        assert sub[0] == 1 and blk == i, sub.hex()
        ngroups = len(packed) // 8
        rawlen = ln - ngroups
        assert 0 < rawlen <= ngroups * 7, (ln, ngroups)
        img += unpack7(packed)[:rawlen]
    return hdr, bytes(img)

# -------------------------------------------------------------------- repack
def repack(img, header=HEADER_MSG_DATA):
    """Build the SMF bytes for a raw image."""
    events = bytearray()
    events += _varlen(0) + b"\xff\x58\x04" + TIMESIG
    events += _varlen(0) + b"\xff\x51\x03" + TEMPO
    def sysex(delta, body):
        return _varlen(delta) + b"\xf0" + _varlen(len(body)) + body
    events += sysex(0, build_msg(header))
    nblocks = (len(img) + RAW_PER_BLOCK - 1) // RAW_PER_BLOCK
    for i in range(nblocks):
        raw = img[i * RAW_PER_BLOCK:(i + 1) * RAW_PER_BLOCK]
        packed = pack7(raw)
        ln = len(raw) + (len(raw) + 6) // 7
        sub = bytes([1, i >> 7, i & 0x7F, ln >> 7, ln & 0x7F])
        events += sysex(BLOCK_DELTA, build_msg(sub + packed))
    events += _varlen(BLOCK_DELTA) + b"\xff\x2f\x00"
    out = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
    out += b"MTrk" + struct.pack(">I", len(events)) + bytes(events)
    return out

# ----------------------------------------------------------------------- CLI
def main(argv):
    if len(argv) < 2:
        print(__doc__); return 2
    cmd = argv[1]
    if cmd == "extract":
        d = open(argv[2], "rb").read()
        hdr, img = extract(d)
        open(argv[3], "wb").write(img)
        print("header:", hdr.hex(), " image:", len(img), "bytes ->", argv[3])
    elif cmd == "repack":
        img = open(argv[2], "rb").read()
        open(argv[3], "wb").write(repack(img))
        print("wrote", argv[3])
    elif cmd == "roundtrip":
        d = open(argv[2], "rb").read()
        hdr, img = extract(d)
        out = repack(img, hdr)
        if out == d:
            print("ROUNDTRIP OK: %d bytes identical, image %d bytes" % (len(d), len(img)))
        else:
            n = next(i for i in range(min(len(d), len(out))) if d[i] != out[i]) if d[:len(out)] != out[:len(d)] else min(len(d), len(out))
            print("ROUNDTRIP FAIL: first difference at offset %#x (orig %d bytes, ours %d bytes)" % (n, len(d), len(out)))
            print(" orig:", d[n - 8:n + 16].hex()); print(" ours:", out[n - 8:n + 16].hex())
            return 1
    else:
        print(__doc__); return 2
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
