// The patcher page: make the file, then send it to the DX. Loaded as a
// module from index.html (no inline script, so the page's Content Security
// Policy allows only this site's own code).
import { build, sha256, messages, PatchError } from './patcher.js';
import { Flasher, FlashError, supported } from './flasher.js';
import { isZip, unzip } from './zip.js';

const $ = (id) => document.getElementById(id);
const short = (h) => h.slice(0, 8) + '…' + h.slice(-7);
const recipes = {};
let index = null;
let url = null;
let made = null;   // { payload, payloadHash, stockHash, recipe }

const canSend = supported();
function layout() {
  $('sendBox').hidden = !canSend || !made;
  $('sendWait').hidden = !canSend || Boolean(made);
  $('noMidi').hidden = canSend;
  $('downloadBox').hidden = !made;
  $('downloadWait').hidden = Boolean(made);
}

const LOAD_FAIL = 'This page couldn\'t load the data it needs. Check your internet connection and reload the page.';
function variant() { return document.querySelector('input[name="variant"]:checked').value; }
// The recipe for the chosen variant, or null (with the status line set) when it cannot be fetched.
async function recipe() {
  const v = variant();
  try {
    if (!recipes[v]) recipes[v] = await (await fetch(`recipes/${v}.json`, { cache: 'no-cache' })).json();
  } catch (e) {
    $('status').textContent = LOAD_FAIL;
    return null;
  }
  return recipes[v];
}
async function loadIndex() {
  index = await (await fetch('recipes/index.json', { cache: 'no-cache' })).json();
  const r = await recipe();
  if (!r) throw new Error(LOAD_FAIL);
  $('footHash').textContent = short(r.payload_sha256);
  chooserState();
}

function setStep(n) { for (let i = 1; i <= 5; i++) $('n' + i).classList.toggle('now', i === n); }
function show(which) { for (const id of ['done', 'wrong', 'mismatch']) $(id).hidden = id !== which; }

// Yamaha's zip holds the .dat inside the app bundle; take whichever entry
// hashes to the stock file, else the first .dat, else the file itself.
async function stockFrom(file, r) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (isZip(bytes)) {
    const entries = await unzip(bytes, (name) => /\.dat$/i.test(name) || /reface updater\.dat$/i.test(name));
    const names = Object.keys(entries);
    for (const name of names) if (await sha256(entries[name]) === r.stock_sha256) return { bytes: entries[name], label: `${file.name} → ${name.split('/').pop()}` };
    if (names.length) return { bytes: entries[names[0]], label: `${file.name} → ${names[0].split('/').pop()}` };
    throw new Error('no reface updater.dat inside');
  }
  return { bytes, label: file.name };
}

async function handle(file) {
  if (!$('accept').checked || !index) return;
  $('status').textContent = 'Making the file…';
  show(null); made = null; layout();
  const r = await recipe();
  if (!r) return;
  let input;
  try { input = await stockFrom(file, r); } catch (e) {
    $('wrongDetail').textContent = `${file.name}: ${e.message}`; show('wrong'); $('status').textContent = ''; return;
  }
  const { bytes, label } = input;
  try {
    const result = await build(bytes, r);
    if (url) URL.revokeObjectURL(url);
    url = URL.createObjectURL(new Blob([result.payload], { type: 'audio/midi' }));
    $('download').href = url;
    $('stockHash').textContent = `${label} · ${bytes.length.toLocaleString()} bytes · fingerprint ${short(result.stockHash)}`;
    $('sameLine').textContent = `${r.title}, checked`;
    $('imageHash').textContent = 'Matches the published file · SHA-256 ' + result.payloadHash;
    made = { ...result, recipe: r };
    layout(); show('done'); setStep(3);
    $('status').textContent = `The "${r.title}" file is ready. Put the DX into Update mode (step 3).`;
    $('install').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    if (e instanceof PatchError && e.kind === 'stock') {
      $('wrongDetail').textContent = `Details: you dropped ${label}, ${bytes.length.toLocaleString()} bytes, fingerprint ${short(e.detail.length === 64 ? e.detail : await sha256(bytes))}. Yamaha's v1.30 updater file has fingerprint ${short(r.stock_sha256)}.`;
      show('wrong');
    } else {
      $('mismatchDetail').textContent = e instanceof PatchError ? `${e.kind}: ${e.detail}` : String(e && e.message || e);
      show('mismatch');
    }
    $('status').textContent = '';
  }
}

function chooserState() {
  const on = $('accept').checked && Boolean(index);
  $('choose').disabled = !on;
  $('drop').classList.toggle('off', !on);
  $('dropNote').textContent = !$('accept').checked ? 'Tick the box above first. The file stays on your computer.' : index ? 'The file stays on your computer.' : 'Loading…';
}
$('accept').addEventListener('change', () => { chooserState(); setStep(made ? 3 : $('accept').checked ? 2 : 1); });
for (const el of document.querySelectorAll('input[name="variant"]')) el.addEventListener('change', async () => {
  made = null; show(null); layout(); $('status').textContent = '';
  const r = await recipe(); if (r) $('footHash').textContent = short(r.payload_sha256);
});
$('choose').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', (e) => { if (e.target.files[0]) handle(e.target.files[0]); e.target.value = ''; });
const drop = $('drop');
drop.addEventListener('dragover', (e) => { e.preventDefault(); if (!$('choose').disabled) drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files[0]) handle(e.dataTransfer.files[0]); });

// Send to the DX.
let flasher = null;
function task(n, text, kind) {
  const t = $('t' + n); const bar = $('bar');
  if (text !== undefined) { t.textContent = text; if (n === 2) t.prepend(bar); }
  t.classList.toggle('err', kind === 'err');
  const li = $('m' + n);
  li.classList.toggle('ok', kind === 'ok'); li.classList.toggle('wait', kind === 'wait'); li.classList.toggle('now', kind === 'now' || kind === 'err');
}
function failText(e) {
  if (e instanceof FlashError) {
    if (e.kind === 'permission') return 'The browser wasn\'t allowed to talk to the DX. Click the icon at the left of the address bar, allow MIDI, and press Connect again.';
    return e.message;
  }
  return (e && e.message) || String(e);
}
async function step(n, fn) {
  const b = $('b' + n); b.disabled = true;
  try { await fn(); } catch (e) { task(n, failText(e), 'err'); b.disabled = false; }
}
// Rebuild the port list, keeping the port the user chose if it is still there.
function fillPorts() {
  const sel = $('port'); const chosen = sel.value; sel.innerHTML = '';
  for (const o of flasher.outputs()) { const opt = document.createElement('option'); opt.value = o.id; opt.textContent = o.name; sel.appendChild(opt); }
  if (chosen && [...sel.options].some((o) => o.value === chosen)) sel.value = chosen;
  return sel.options.length;
}
$('b1').addEventListener('click', () => step(1, async () => {
  task(1, 'Connecting…', 'now');
  flasher = await Flasher.connect();
  flasher.access.addEventListener('statechange', () => fillPorts());
  if (!fillPorts()) throw new FlashError('absent', 'No reface found on USB. Check the cable, that the DX is in Update mode and that no other program is using it, then press Connect again.');
  task(1, 'Connected.', 'ok');
  $('port').disabled = false; $('b2').disabled = false; task(2, undefined, 'now');
}));
$('b2').addEventListener('click', () => step(2, async () => {
  const out = flasher.access.outputs.get($('port').value);
  if (!out) throw new FlashError('absent', 'That port is gone. Press Connect again.');
  const msgs = messages(made.payload, made.recipe.smf);
  const bar = $('bar'); bar.hidden = false; bar.max = msgs.length; bar.value = 0;
  $('port').disabled = true;
  task(2, `Sending block 0 of ${msgs.length}…`, 'now'); $('t2').prepend(bar);
  try {
    await flasher.send(out, msgs, (i, n) => { bar.value = i; $('t2').lastChild.textContent = `Sending block ${i} of ${n}… about ${Math.ceil((n - i) * 1.4 / 60)} min left`; });
  } finally { $('port').disabled = false; }
  task(2, 'All blocks sent. Watch the DX display.', 'ok'); $('t2').prepend(bar);
  $('sendNext').hidden = false; setStep(5);
}));

layout();
loadIndex().catch(() => { $('status').textContent = LOAD_FAIL; });
