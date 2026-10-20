// Send the updater payload to a reface DX in Update mode, over USB MIDI
// (Web MIDI with SysEx): Chrome and Edge on a computer. It sends exactly the
// 139 SysEx messages Yamaha's updater sends, with the same gaps between them
// (the SMF's delta times: about 1.36 s per block, 3 minutes in all).

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function supported(nav = globalThis.navigator, win = globalThis) {
  return Boolean(nav && nav.requestMIDIAccess && win.isSecureContext !== false);
}

export class FlashError extends Error {
  constructor(kind, message) { super(message); this.kind = kind; }
}

export class Flasher {
  constructor(access) { this.access = access; this.cancelled = false; }

  static async connect(nav = globalThis.navigator) {
    let access;
    try { access = await nav.requestMIDIAccess({ sysex: true }); } catch (e) {
      throw new FlashError('permission', 'MIDI access was not allowed');
    }
    return new Flasher(access);
  }

  // Output ports that look like a reface. Update mode keeps the same name.
  outputs() {
    return [...this.access.outputs.values()].filter((p) => /reface/i.test(`${p.name} ${p.manufacturer}`) && p.state !== 'disconnected');
  }

  // Send every message in order with its delay before it. progress(i, n)
  // after each message.
  async send(output, msgs, progress = () => {}) {
    this.cancelled = false;
    try { await output.open(); } catch (e) {
      throw new FlashError('busy', 'Could not open the reface port. Close Yamaha\'s updater and any other MIDI software, then try again.');
    }
    for (let i = 0; i < msgs.length; i++) {
      if (this.cancelled) throw new FlashError('cancelled', 'Stopped.');
      if (msgs[i].delay) await sleep(msgs[i].delay);
      try { output.send(msgs[i].data); } catch (e) {
        throw new FlashError('send', `The reface stopped accepting data at block ${i} of ${msgs.length}: ${e.message}`);
      }
      progress(i + 1, msgs.length);
    }
    // the file ends with the same gap before end-of-track; give the DX that time too
    await sleep(msgs[msgs.length - 1].delay || 1400);
  }

  cancel() { this.cancelled = true; }
}
