/**
 * Behavioural tests for the fetch wrapper's timeout.
 *
 * Why this exists: an upload sat on "Starting..." forever. The server had closed
 * the idle keep-alive connection, the browser reused it anyway, and a POST on a
 * dead socket is never retried - a POST is not safe to repeat. With no timeout
 * the promise simply never settled, so the UI had nothing to report and the
 * whole queue stalled behind it showing "Waiting...".
 *
 * Invoked by tests/test_upload_stall.py with the module URL as argv[2].
 */
import assert from 'node:assert/strict';

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

const store = new Map([
  ['lanshare.device_id', 'device-1'],
  ['lanshare.device_token', 'token-1'],
]);
globalThis.localStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
};
globalThis.XMLHttpRequest = class {};
globalThis.Blob = class {};

const api = await import(process.argv[2]);
const drain = () => new Promise((resolve) => realSetTimeout(resolve, 5));
const settled = (p) => p.then((value) => ({ value }), (error) => ({ error }));

const tests = {
  'a request to a dead connection does not hang forever': async () => {
    // The real failure: the socket is gone, so nothing ever comes back and no
    // error is raised either. Exactly what the phone did.
    globalThis.fetch = (_path, options) =>
      new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => {
          const error = new Error('aborted');
          error.name = 'AbortError';
          reject(error);
        });
      });

    const outcome = settled(api.createTransfer({
      filename: 'video.mp4', size: 9_400_000, receiverId: 'device-2',
    }));

    advance(api.REQUEST_TIMEOUT_MS + 1);
    const { error } = await outcome;

    assert.ok(error, 'a request to a dead connection must not hang forever');
    assert.match(
      error.message,
      /did not respond|try again/i,
      `the message must tell the user what to do, got: ${error.message}`
    );
  },

  'a request that answers in time is untouched': async () => {
    globalThis.fetch = async () => ({
      ok: true,
      status: 200,
      json: async () => ({ transfer_id: 't1', chunk_size: 4194304, total_chunks: 3 }),
    });

    const outcome = settled(api.createTransfer({
      filename: 'video.mp4', size: 9_400_000, receiverId: 'device-2',
    }));
    await drain();

    const { value, error } = await outcome;
    assert.equal(error, undefined, `a healthy request was broken: ${error && error.message}`);
    assert.deepEqual(value, { transfer_id: 't1', chunk_size: 4194304, total_chunks: 3 });
  },

  'a slow but live request is not cut off early': async () => {
    let settle;
    globalThis.fetch = () => new Promise((resolve) => {
      settle = () => resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
    });

    const outcome = settled(api.createTransfer({
      filename: 'video.mp4', size: 1, receiverId: 'device-2',
    }));

    advance(api.REQUEST_TIMEOUT_MS - 1);
    await drain();
    settle();

    const { error } = await outcome;
    assert.equal(error, undefined, `a request answering just in time was killed: ${error}`);
  },

  'the timeout is cleared when the request succeeds': async () => {
    // A timer left armed fires an abort at a request that already finished.
    globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
    await api.createTransfer({ filename: 'a.mp4', size: 1, receiverId: 'd2' });
    assert.equal(timers.size, 0, `${timers.size} timer(s) left armed after a successful request`);
  },
};

let passed = 0;
let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  timers.clear();
  now = 0;
  try {
    await Promise.race([
      fn(),
      new Promise((_r, reject) =>
        realSetTimeout(() => reject(new Error('hung: the promise never settled')), 2000)
      ),
    ]);
    console.log(`  ok   ${name}`);
    passed += 1;
  } catch (error) {
    console.log(`  FAIL ${name}\n       ${error.message}`);
    failed += 1;
  }
}
console.log(`${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
