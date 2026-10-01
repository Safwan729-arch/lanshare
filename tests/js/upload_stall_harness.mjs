/**
 * Behavioural tests for `uploadChunk`, run under node with a fake XHR.
 *
 * Why this exists: a 47 MiB video from an iPhone froze at 8% - one chunk in -
 * and stayed there. A hung TCP connection fires no XHR event at all, so the
 * promise never settled, the retry loop never ran, and the UI showed a patient
 * spinner forever. Only a timer can notice silence, and these tests are the
 * only way to prove the timer behaves.
 *
 * Invoked by tests/test_upload_stall.py, which passes the module URL as argv[2]
 * so the shipped `api.js` is what gets exercised, not a copy of it.
 */
import assert from 'node:assert/strict';

// -- a clock we control, so the tests are deterministic and instant ----------
// Captured before the override: the harness itself needs a real timer to notice
// a test that hangs, which is precisely the failure mode under test.
const realSetTimeout = globalThis.setTimeout;

let now = 0;
let nextId = 0;
const timers = new Map();
globalThis.setTimeout = (fn, ms) => {
  const id = (nextId += 1);
  timers.set(id, { at: now + ms, fn });
  return id;
};
globalThis.clearTimeout = (id) => timers.delete(id);

function advance(ms) {
  now += ms;
  for (const [id, timer] of [...timers].sort((a, b) => a[1].at - b[1].at)) {
    if (timer.at <= now) {
      timers.delete(id);
      timer.fn();
    }
  }
}

// -- just enough browser ----------------------------------------------------
const store = new Map([
  ['lanshare.device_id', 'device-1'],
  ['lanshare.device_token', 'token-1'],
]);
globalThis.localStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
};

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

let live = null;

class FakeXHR extends FakeTarget {
  constructor() {
    super();
    this.upload = new FakeTarget();
    this.aborted = false;
    this.status = 0;
    this.responseText = '';
    this.readyState = 0;
    this.headers = {};
    live = this;
  }

  getResponseHeader(name) {
    const key = Object.keys(this.headers).find((k) => k.toLowerCase() === name.toLowerCase());
    return key ? this.headers[key] : null;
  }

  /** The iOS failure: a complete response that never advances past LOADING. */
  deliverButNeverFinish(body) {
    this.status = 200;
    this.responseText = body;
    this.headers['Content-Length'] = String(body.length);
    this.readyState = 3;
  }

  open() {}
  setRequestHeader() {}
  send(body) {
    this.sent = true;
    this.body = body;
  }

  abort() {
    this.aborted = true;
    this.fire('abort');
  }
}
globalThis.XMLHttpRequest = FakeXHR;

/**
 * Enough of Blob for `blob instanceof Blob` in the body-kind diagnostic.
 *
 * Note what this is NOT for: sending plain memory instead of a Blob was tried
 * on the device and changed nothing - a buffer body hung exactly as a slice
 * did. What the body is made of is not what breaks these uploads.
 */
class FakeBlob {
  constructor(bytes) {
    this.size = bytes;
  }

  async arrayBuffer() {
    return new ArrayBuffer(this.size);
  }
}
globalThis.Blob = FakeBlob;

const { uploadChunk } = await import(process.argv[2]);

const STALL = 1000;
const FIRST_BYTE = 200; // the shorter window, for a request that never starts
const settled = (promise) => promise.then((value) => ({ value }), (error) => ({ error }));
const start = (extra = {}) =>
  uploadChunk({
    transferId: 'transfer-1',
    index: 1,
    blob: 'body',
    stallMs: STALL,
    firstByteMs: FIRST_BYTE,
    ...extra,
  });

/** Let any already-resolved promise callbacks run, without moving the clock. */
const drain = () => new Promise((resolve) => realSetTimeout(resolve, 5));

const tests = {
  'a response that never completes is taken from the body anyway': async () => {
    // Measured on an iPhone, every attempt: the whole response is present at
    // readyState 3 within ~131ms, and `load` never fires. Waiting for an event
    // Safari will not send is what hung every multi-chunk upload.
    const { promise } = start();
    const outcome = settled(promise);

    live.upload.fire('progress', { lengthComputable: true, loaded: 4194304 });
    live.upload.fire('load');
    live.deliverButNeverFinish('{"bytes_written":4194304}');

    advance(200);

    const { value, error } = await outcome;
    assert.equal(error, undefined, `a complete response was not used: ${error && error.message}`);
    assert.deepEqual(value, { bytes_written: 4194304 });
    assert.ok(live.aborted, 'the dead request must be torn down to free the connection');
  },

  'a half-arrived response is not mistaken for a complete one': async () => {
    // Resolving on a truncated body would hand upload.js malformed JSON and
    // mark a chunk that never finished as sent.
    const { promise } = start();
    const outcome = settled(promise);
    let finished = false;
    outcome.then(() => {
      finished = true;
    });

    live.upload.fire('progress', { lengthComputable: true, loaded: 4194304 });
    live.upload.fire('load');
    live.status = 200;
    live.responseText = '{"bytes_wri';
    live.headers['Content-Length'] = '25';
    live.readyState = 3;

    advance(200);
    await drain();
    assert.equal(finished, false, 'resolved on a partial body');

    advance(STALL + 1);
    const { error } = await outcome;
    assert.ok(error, 'a response that really never arrives must still time out');
  },

  'a non-2xx response delivered this way still fails': async () => {
    const { promise } = start();
    const outcome = settled(promise);
    live.upload.fire('progress', { lengthComputable: true, loaded: 4194304 });
    live.upload.fire('load');
    live.status = 409;
    live.responseText = '{"detail":"Transfer is cancelled"}';
    live.headers['Content-Length'] = '34';
    live.readyState = 3;

    advance(200);

    const { error } = await outcome;
    assert.ok(error, 'a 409 read from the body must reject');
    assert.match(error.message, /cancelled/i, `lost the server's reason: ${error.message}`);
  },

  'a connection that goes silent is given up on': async () => {
    const { promise } = start();
    const outcome = settled(promise);
    live.upload.fire('progress', { lengthComputable: true, loaded: 4096 });

    advance(STALL + 1);

    const { error } = await outcome;
    assert.ok(error, 'a stalled chunk must reject rather than hang forever');
    assert.match(error.message, /stall/i, `unhelpful message: ${error.message}`);
    assert.ok(live.aborted, 'the dead request must actually be torn down');
  },

  'a stall is not reported as a cancellation': async () => {
    // upload.js rethrows AbortError as "the user cancelled" and stops the whole
    // transfer. A stall must stay retryable, so it must not wear that name.
    const { promise } = start();
    const outcome = settled(promise);
    advance(FIRST_BYTE + 1);

    const { error } = await outcome;
    assert.notEqual(error.name, 'AbortError', 'a stall must not look like a user cancel');
    assert.equal(error.name, 'StallError');
  },

  'a request that never sends a byte gives up on the short window': async () => {
    // The iOS case: the slice is unreadable, so the request is never dispatched
    // and no event ever fires. Waiting the full stall window only delays the
    // recovery - nothing has moved on a LAN, so nothing is going to.
    const { promise } = start();
    const outcome = settled(promise);

    advance(FIRST_BYTE + 1);

    const { error } = await outcome;
    assert.ok(error, `a dead request must not wait ${STALL}ms to be noticed`);
    assert.match(error.message, /not a single byte/i, `unclear message: ${error.message}`);
  },

  'an upload that has started gets the longer window': async () => {
    // Bytes have moved, so this may just be a bad link. Patience is warranted.
    const { promise } = start();
    const outcome = settled(promise);
    let finished = false;
    outcome.then(() => {
      finished = true;
    });

    live.upload.fire('progress', { lengthComputable: true, loaded: 4096 });
    advance(FIRST_BYTE + 1);
    await drain();

    assert.equal(finished, false, 'a slow but live upload was killed on the short window');

    advance(STALL + 1);
    const { error } = await outcome;
    assert.ok(error, 'it must still time out eventually');
  },

  'a slow upload that keeps moving is left alone': async () => {
    const { promise } = start();
    const outcome = settled(promise);

    // The first byte moves promptly, which is what earns the longer window.
    live.upload.fire('progress', { lengthComputable: true, loaded: 4096 });

    // Then: ten times longer than the stall window, but never silent for it.
    for (let i = 0; i < 10; i += 1) {
      advance(STALL - 100);
      live.upload.fire('progress', { lengthComputable: true, loaded: (i + 2) * 4096 });
    }

    live.status = 200;
    live.responseText = '{"bytes_written":4194304}';
    live.fire('load');

    const { value, error } = await outcome;
    assert.equal(error, undefined, `a live upload was killed: ${error && error.message}`);
    assert.deepEqual(value, { bytes_written: 4194304 });
    assert.equal(live.aborted, false);
  },

  'silence while waiting for the response is also a stall': async () => {
    // Once the body is out no further upload events arrive, so the watchdog
    // has to be re-armed or a server that never answers hangs the transfer.
    const { promise } = start();
    const outcome = settled(promise);
    live.upload.fire('progress', { lengthComputable: true, loaded: 4194304 });
    live.upload.fire('load');

    advance(STALL + 1);

    const { error } = await outcome;
    assert.ok(error, 'a server that accepts the body and never answers must time out');
    assert.notEqual(error.name, 'AbortError');
  },

  'a real cancel still reports AbortError': async () => {
    const { promise, abort } = start();
    const outcome = settled(promise);

    abort();

    const { error } = await outcome;
    assert.equal(error.name, 'AbortError', 'cancel must stay distinguishable from a stall');
  },

  'progress is still reported to the caller': async () => {
    const seen = [];
    const { promise } = start({ onProgress: (loaded) => seen.push(loaded) });
    const outcome = settled(promise);

    live.upload.fire('progress', { lengthComputable: true, loaded: 1024 });
    live.upload.fire('progress', { lengthComputable: true, loaded: 2048 });
    advance(STALL + 1);
    await outcome;

    assert.deepEqual(seen, [1024, 2048], 'the watchdog must not swallow progress events');
  },

  'the watchdog does not outlive the request': async () => {
    const { promise } = start();
    const outcome = settled(promise);
    live.status = 200;
    live.responseText = '{}';
    live.fire('load');
    await outcome;

    assert.equal(timers.size, 0, 'a settled request left a timer running');
  },
};

let failures = 0;
for (const [name, run] of Object.entries(tests)) {
  now = 0;
  timers.clear();
  live = null;
  try {
    // Without this a never-settling promise deadlocks the whole run and node
    // reports "unsettled top-level await" instead of naming the broken test.
    await Promise.race([
      run(),
      new Promise((_, reject) =>
        realSetTimeout(() => reject(new Error('hung: the promise never settled')), 2000)
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
