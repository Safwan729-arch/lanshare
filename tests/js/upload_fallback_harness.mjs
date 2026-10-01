/**
 * End-to-end tests for `Upload`, driving the real upload.js and api.js.
 *
 * Why this exists: an iPhone sending a 47 MiB video uploaded chunk 0 and then
 * never issued the request for chunk 1 - the server saw not one byte, so the
 * whole failure lived in the browser's read of the file. A file picked from the
 * iOS Photos library is exported lazily, and a slice past the exported part can
 * simply never yield data.
 *
 * `FakeFile` reproduces exactly that: slices at offset 0 are readable, slices
 * beyond it hand back a body the transport never manages to send.
 *
 * Invoked by tests/test_upload_stall.py, which passes the module URL as argv[2].
 */
import assert from 'node:assert/strict';

// Real timers, but compressed: the watchdog waits 30s and the retry backoff
// 600ms, and the ordering between them is what matters, not the wall clock.
const realSetTimeout = globalThis.setTimeout;
const SPEEDUP = 500;
globalThis.setTimeout = (fn, ms = 0) => realSetTimeout(fn, Math.max(1, Math.round(ms / SPEEDUP)));

const store = new Map([
  ['lanshare.device_id', 'device-1'],
  ['lanshare.device_token', 'token-1'],
]);
globalThis.localStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
};

const SIZE = 49478774; // the video from the bug report
const CHUNK = 4194304;
const TOTAL = Math.ceil(SIZE / CHUNK);

/** A file whose slices past `poisonAfter` produce a body that never sends. */
class FakeFile {
  constructor({ poisonAfter = Infinity, readable = true } = {}) {
    this.size = SIZE;
    this.name = 'Detail_20260823202036.mp4';
    this.type = 'video/mp4';
    this.poisonAfter = poisonAfter;
    this.readable = readable;
    this.slices = [];
    this.wholeReads = 0;
  }

  slice(start, end) {
    this.slices.push(start);
    return { poisoned: start >= this.poisonAfter, size: end - start };
  }

  async arrayBuffer() {
    this.wholeReads += 1;
    if (!this.readable) throw new Error('the file could not be read');
    return new ArrayBuffer(this.size);
  }
}

const sent = [];

class FakeTarget {
  constructor() {
    this.handlers = new Map();
  }

  addEventListener(type, fn) {
    if (!this.handlers.has(type)) this.handlers.set(type, []);
    this.handlers.get(type).push(fn);
  }

  fire(type, event = {}) {
    for (const fn of this.handlers.get(type) || []) fn(event);
  }
}

class FakeXHR extends FakeTarget {
  constructor() {
    super();
    this.upload = new FakeTarget();
    this.status = 0;
    this.responseText = '';
  }

  open(method, url) {
    this.url = url;
  }

  setRequestHeader() {}
  abort() {
    this.fire('abort');
  }

  send(body) {
    // A poisoned slice is what iOS hands back: the transport never gets any
    // data, so no event of any kind is ever fired. Only the watchdog notices.
    if (body && body.poisoned) return;

    realSetTimeout(() => {
      const size = body.size ?? body.byteLength;
      sent.push({ url: this.url, size });
      this.upload.fire('progress', { lengthComputable: true, loaded: size });
      this.upload.fire('load');
      this.status = 200;
      this.responseText = JSON.stringify({ bytes_written: size });
      this.fire('load');
    }, 1);
  }
}
globalThis.XMLHttpRequest = FakeXHR;

globalThis.fetch = async (path, options = {}) => {
  const json = (data) => ({ ok: true, status: 200, json: async () => data });
  if (path === '/api/transfers' && options.method === 'POST') {
    return json({ transfer_id: 'transfer-1', chunk_size: CHUNK, total_chunks: TOTAL });
  }
  if (path.endsWith('/complete')) return json({ sha256: 'deadbeef', status: 'completed' });
  if (path.startsWith('/api/transfers/')) {
    return json({ received_chunks: [], missing_chunks: [], chunk_size: CHUNK });
  }
  throw new Error(`unexpected fetch: ${path}`);
};

const { Upload } = await import(process.argv[2]);

const run = async (file) => {
  sent.length = 0;
  const upload = new Upload(file, 'receiver-1');
  await upload.start();
  return upload;
};

const tests = {
  'an ordinary file never gets buffered into memory': async () => {
    const file = new FakeFile();
    const upload = await run(file);

    assert.equal(upload.status, 'completed', upload.error || '');
    assert.equal(sent.length, TOTAL, `sent ${sent.length} of ${TOTAL} chunks`);
    assert.equal(file.wholeReads, 0, 'the happy path must not read the whole file into memory');
  },

  'a file that stops yielding slices is read whole and finishes': async () => {
    // Chunk 0 uploads, every later slice is dead: the exact bug report.
    const file = new FakeFile({ poisonAfter: CHUNK });
    const upload = await run(file);

    assert.equal(upload.status, 'completed', `stalled: ${upload.error}`);
    assert.equal(file.wholeReads, 1, 'the file should be read whole exactly once');
    assert.equal(sent.length, TOTAL, `only ${sent.length} of ${TOTAL} chunks were sent`);
    assert.equal(upload.progress, 1);
  },

  'the recovery does not re-read the file for every later chunk': async () => {
    const file = new FakeFile({ poisonAfter: CHUNK });
    await run(file);
    assert.equal(file.wholeReads, 1, `read the whole file ${file.wholeReads} times`);
  },

  'a file too large to hold is not buffered': async () => {
    const file = new FakeFile({ poisonAfter: CHUNK });
    file.size = 400 * 1024 * 1024; // over BUFFER_FALLBACK_LIMIT
    const upload = await run(file);

    assert.equal(upload.status, 'failed');
    assert.equal(file.wholeReads, 0, 'a huge file must not be pulled into memory');
    assert.match(upload.error, /stall/i);
  },

  'an unreadable file fails honestly instead of hanging': async () => {
    const file = new FakeFile({ poisonAfter: CHUNK, readable: false });
    const upload = await run(file);

    assert.equal(upload.status, 'failed');
    assert.ok(upload.error, 'a failed upload must carry a message');
  },

  'a cancelled upload still reports cancelled': async () => {
    // Cancelled mid-flight, not before the transfer exists: `start` sets the
    // status to 'uploading' after creating it, so cancelling inside that window
    // is overwritten. See the handoff note - real, minor, and not this fix.
    const file = new FakeFile();
    const upload = new Upload(file, 'receiver-1');
    const started = upload.start();
    await new Promise((resolve) => realSetTimeout(resolve, 20));

    await upload.cancel();
    await started;

    assert.equal(upload.status, 'cancelled');
    assert.ok(sent.length < TOTAL, 'cancelling must stop the remaining chunks');
  },
};

let failures = 0;
for (const [name, test] of Object.entries(tests)) {
  try {
    await Promise.race([
      test(),
      new Promise((_, reject) =>
        realSetTimeout(() => reject(new Error('hung: the upload never settled')), 10000)
      ),
    ]);
    console.log(`  ok   ${name}`);
  } catch (error) {
    failures += 1;
    console.log(`  FAIL ${name}\n       ${error.message}`);
  }
}
console.log(`${Object.keys(tests).length - failures} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
