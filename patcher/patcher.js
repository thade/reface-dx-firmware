// Builds the patched updater payload from Yamaha's own "reface updater.dat"
// and a recipe (reface_csdx_fw/tools/export_recipe.py writes recipes; the
// reference implementation is patch_clock_menu.py). Runs in the browser and
// in Node; nothing is uploaded. The input must hash to the recipe's stock
// hash and both results (the raw image and the SysEx payload) to the
// recipe's hashes, or nothing is returned.

export class PatchError extends Error {
  constructor(kind, detail) { super(kind); this.kind = kind; this.detail = detail; }
}

export async function sha256(bytes) {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  return Array.from(digest, (b) => b.toString(16).padStart(2, '0')).join('');
}

function unbase64(text) {
  const raw = atob(text);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

// ---- the updater payload: a format-0 SMF of Yamaha bulk-dump SysEx ------
const HDR = [0x43, 0x00, 0x7f, 0x1c];
const ADDRESS = [0x03, 0x50, 0x00];
const RAW_PER_BLOCK = 14322;
const HEADER_MSG = [0x00, 0x01, 0x0a, 0x00, 0x00, 0x10, 0x08, 0x00, 0x00];
const BLOCK_DELTA = 1309;

function varlen(v) {
  const out = [v & 0x7f]; v >>>= 7;
  while (v) { out.push(0x80 | (v & 0x7f)); v >>>= 7; }
  return out.reverse();
}
function readVarlen(d, p) {
  let v = 0;
  for (;;) { const b = d[p++]; v = (v << 7) | (b & 0x7f); if (!(b & 0x80)) return [v, p]; }
}
function checksum(bytes) { let s = 0; for (const b of bytes) s += b; return (-s) & 0x7f; }

// Every SysEx body (after F0, including F7) in the single track.
export function parseSMF(d) {
  if (String.fromCharCode(...d.subarray(0, 4)) !== 'MThd') throw new PatchError('stock', 'not a Standard MIDI File');
  let p = 14;
  if (String.fromCharCode(...d.subarray(p, p + 4)) !== 'MTrk') throw new PatchError('stock', 'no track');
  const len = (d[p + 4] << 24 | d[p + 5] << 16 | d[p + 6] << 8 | d[p + 7]) >>> 0; p += 8;
  const end = p + len; const msgs = []; let running = null; let open = false;
  while (p < end) {
    let dt; [dt, p] = readVarlen(d, p);
    const st = d[p];
    if (st === 0xf0 || st === 0xf7) {
      p++; let ln; [ln, p] = readVarlen(d, p);
      const body = d.subarray(p, p + ln); p += ln;
      if (st === 0xf0) { msgs.push({ dt, body: Array.from(body) }); open = body[ln - 1] !== 0xf7; }
      else if (open) { msgs[msgs.length - 1].body.push(...body); open = body[ln - 1] !== 0xf7; }
    } else if (st === 0xff) {
      p += 2; let ln; [ln, p] = readVarlen(d, p); p += ln;
    } else {
      if (st & 0x80) { running = st; p++; }
      const hi = running >> 4;
      p += (hi === 0xc || hi === 0xd) ? 1 : hi === 0xf ? ({ 0xf1: 1, 0xf2: 2, 0xf3: 1 }[running] || 0) : 2;
    }
  }
  return msgs;
}

function splitMsg(body) {
  if (!HDR.every((b, i) => body[i] === b)) throw new PatchError('stock', 'not a reface bulk message');
  const cnt = (body[4] << 7) | body[5];
  if (!ADDRESS.every((b, i) => body[6 + i] === b)) throw new PatchError('stock', 'wrong bulk address');
  const data = body.slice(9, -2);
  if (body[body.length - 1] !== 0xf7 || cnt !== 3 + data.length) throw new PatchError('stock', 'bad bulk framing');
  if (checksum(body.slice(6, -2)) !== body[body.length - 2]) throw new PatchError('stock', 'bad bulk checksum');
  return data;
}
function buildMsg(data) {
  const cnt = 3 + data.length;
  return [...HDR, cnt >> 7, cnt & 0x7f, ...ADDRESS, ...data, checksum([...ADDRESS, ...data]), 0xf7];
}
function unpack7(p) {
  const out = [];
  for (let i = 0; i < p.length; i += 8) {
    const msb = p[i];
    for (let j = 0; j < 7; j++) out.push(p[i + 1 + j] | (msb & (1 << (6 - j)) ? 0x80 : 0));
  }
  return out;
}
function pack7(raw) {
  const out = [];
  for (let i = 0; i < raw.length; i += 7) {
    const grp = Array.from(raw.subarray(i, i + 7)); while (grp.length < 7) grp.push(0);
    let msb = 0; grp.forEach((b, j) => { if (b & 0x80) msb |= 1 << (6 - j); });
    out.push(msb, ...grp.map((b) => b & 0x7f));
  }
  return out;
}

// The raw 1,966,080-byte image inside the payload.
export function extract(dat) {
  const msgs = parseSMF(dat);
  splitMsg(msgs[0].body);
  const parts = [];
  msgs.slice(1).forEach((m, i) => {
    const data = splitMsg(m.body);
    const blk = (data[1] << 7) | data[2]; const ln = (data[3] << 7) | data[4];
    if (data[0] !== 1 || blk !== i) throw new PatchError('stock', 'block order');
    const packed = data.slice(5); const ngroups = packed.length >> 3;
    parts.push(unpack7(packed).slice(0, ln - ngroups));
  });
  const img = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0; for (const p of parts) { img.set(p, at); at += p.length; }
  return img;
}

// The SMF bytes for an image: the same file Yamaha ships, byte for byte,
// when the image is the stock one.
export function repack(img) {
  const ev = [];
  ev.push(...varlen(0), 0xff, 0x58, 0x04, 0x04, 0x02, 0x18, 0x08);
  ev.push(...varlen(0), 0xff, 0x51, 0x03, 0x07, 0xa1, 0x20);
  const sysex = (delta, body) => ev.push(...varlen(delta), 0xf0, ...varlen(body.length), ...body);
  sysex(0, buildMsg(HEADER_MSG));
  const nblocks = Math.ceil(img.length / RAW_PER_BLOCK);
  for (let i = 0; i < nblocks; i++) {
    const raw = img.subarray(i * RAW_PER_BLOCK, (i + 1) * RAW_PER_BLOCK);
    const ln = raw.length + Math.floor((raw.length + 6) / 7);
    sysex(BLOCK_DELTA, buildMsg([1, i >> 7, i & 0x7f, ln >> 7, ln & 0x7f, ...pack7(raw)]));
  }
  ev.push(...varlen(BLOCK_DELTA), 0xff, 0x2f, 0x00);
  const out = new Uint8Array(14 + 8 + ev.length);
  out.set([0x4d, 0x54, 0x68, 0x64, 0, 0, 0, 6, 0, 0, 0, 1, 1, 224], 0);          // MThd, format 0, 1 track, 480 ppqn
  out.set([0x4d, 0x54, 0x72, 0x6b, ev.length >>> 24, (ev.length >>> 16) & 0xff, (ev.length >>> 8) & 0xff, ev.length & 0xff], 14);
  out.set(ev, 22);
  return out;
}

// ---- title bars (rebuilt from the user's own stock bitmaps) --------------
function unpackBitmap(bytes, w, h) {
  const stride = (w + 7) >> 3;
  return Array.from({ length: h }, (_, y) => Array.from({ length: w }, (_, x) => (bytes[y * stride + (x >> 3)] >> (7 - (x & 7))) & 1));
}
function packBitmap(rows, w) {
  const stride = (w + 7) >> 3; const out = new Uint8Array(rows.length * stride);
  rows.forEach((row, y) => { for (let x = 0; x < w; x++) if (row[x]) out[y * stride + (x >> 3)] |= 0x80 >> (x & 7); });
  return out;
}
function retitle(rows, page, t) {
  const out = Array.from({ length: t.height }, () => new Array(t.width).fill(0));
  for (let y = 0; y < t.height; y++) for (let x = 0; x < 88; x++) if (rows[y][x] && x - t.shift >= 0) out[y][x - t.shift] = 1;
  for (let i = 0; i < t.pages; i++) {
    const g = i === page ? t.dots.filled : t.dots.empty;
    g.forEach((line, gy) => [...line].forEach((c, gx) => { if (c === '#') out[t.dots.y + gy][t.dots.x0 + i * t.dots.pitch + gx] = 1; }));
  }
  return out;
}
function applyTitles(image, stock, recipe) {
  const t = recipe.titles; const size = t.width * t.height / 8;
  const stockRows = t.at.map((at) => unpackBitmap(stock.subarray(at, at + size), t.width, t.height));
  t.at.forEach((at, i) => image.set(packBitmap(retitle(stockRows[i], i, t), t.width), at));
  const m = recipe.midi2_title;
  const rows = Array.from({ length: t.height }, () => new Array(t.width).fill(0));
  for (let y = 0; y < t.height; y++) for (let x = m.columns[0]; x < m.columns[1]; x++) rows[y][x] = stockRows[m.from_title][y][x];
  m.glyph.rows.forEach((line, gy) => [...line].forEach((c, gx) => { if (c === '#') rows[m.glyph.y + gy][m.glyph.x + gx] = 1; }));
  image.set(packBitmap(retitle(rows, t.pages - 1, t), t.width), m.at);
}

// ---- the build ------------------------------------------------------------
// stock: the bytes of Yamaha's "reface updater.dat". Returns the patched
// payload (same format, ready to send) and the raw image.
export async function build(stock, recipe) {
  if (recipe.format !== 1) throw new PatchError('recipe', String(recipe.format));
  const stockHash = await sha256(stock);
  if (stockHash !== recipe.stock_sha256) throw new PatchError('stock', stockHash);
  const stockImage = extract(stock);
  if (stockImage.length !== recipe.image_size || await sha256(stockImage) !== recipe.stock_image_sha256) throw new PatchError('stock', 'image');
  const image = Uint8Array.from(stockImage);
  for (const op of recipe.ops) {
    if (op[0] === 'put') image.set(unbase64(op[2]), op[1]);
    else throw new PatchError('recipe', op[0]);
  }
  if (recipe.titles) applyTitles(image, stockImage, recipe);
  const hash = await sha256(image);
  if (hash !== recipe.image_sha256) throw new PatchError('result', hash);
  const payload = repack(image);
  const payloadHash = await sha256(payload);
  if (payloadHash !== recipe.payload_sha256 || payload.length !== recipe.payload_size) throw new PatchError('result', payloadHash);
  return { image, hash, payload, payloadHash, stockHash };
}

// The SysEx messages of a payload with the delay (ms) before each, for the sender.
export function messages(payload, smf) {
  const msPerTick = smf.tempo_us / smf.ppqn / 1000;
  return parseSMF(payload).map((m) => ({ delay: m.dt * msPerTick, data: Uint8Array.from([0xf0, ...m.body]) }));
}
