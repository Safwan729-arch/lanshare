/**
 * The sender's half of consent: an upload that waits to be allowed.
 *
 * Three things must hold, and the first is the one that would be easy to get
 * wrong: while waiting, not one byte may be sent. The other two are that a
 * refusal and a silence each end the upload with something a person can act on,
 * rather than a progress bar that never moves.
 *
 * Invoked by tests/test_upload_stall.py with the module URL as argv[2].
 */
import assert from 'node:assert/strict';

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

const { Upload } = await import(process.argv[2]);

class FakeFile {
  constructor(size = 1000) {
    this.size = size;
    this.name = 'photo.jpg';
    this.type = 'image/jpeg';
  }

  slice(start, end) {
    return { size: end - start };
  }
}

/**
 * An Upload whose server calls are replaced, so the test drives the decision.
 *
 * `statuses` is what `getTransfer` returns on each successive poll, which is
 * how a test says "they thought about it for two seconds, then accepted".
 */
function makeUpload(statuses) {
  const upload = new Upload(new FakeFile(), 'receiver-1', {});
  const chunks = [];
  const waits = [];
  upload.deps = {
    createTransfer: async () => ({
      transfer_id: 't1',
      chunk_size: 1000,
      total_chunks: 1,
      status: 'awaiting',
    }),
    getTransfer: async () => ({ status: statuses.shift() ?? 'awaiting', received_chunks: [] }),
    uploadChunk: ({ index }) => {
      chunks.push(index);
      return { promise: Promise.resolve(), abort() {} };
    },
    completeTransfer: async () => ({ sha256: 'abc' }),
    sleep: async (ms) => {
      waits.push(ms);
    },
  };
  return { upload, chunks, waits };
}

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push([true, name]);
  } catch (error) {
    results.push([false, `${name}: ${error.message}`]);
  }
}

await check('nothing is sent while the recipient is deciding', async () => {
  const { upload, chunks } = makeUpload(['awaiting', 'awaiting', 'pending']);
  await upload.start();
  assert.equal(upload.status, 'completed', `ended as ${upload.status}: ${upload.error}`);
  assert.deepEqual(chunks, [0], 'exactly one chunk, and only after acceptance');
});

await check('the waiting shows in the UI rather than looking like a stall', async () => {
  const seen = [];
  const { upload } = makeUpload(['awaiting', 'pending']);
  upload.callbacks.onChange = (current) => seen.push(current.status);
  await upload.start();
  assert.ok(seen.includes('waiting'), `expected a waiting status, saw ${seen.join()}`);
});

await check('a refusal ends the upload with a reason', async () => {
  const { upload, chunks } = makeUpload(['declined']);
  await upload.start();
  assert.equal(upload.status, 'failed');
  assert.match(upload.error, /declined/i);
  assert.deepEqual(chunks, [], 'a refused file must never send a byte');
});

await check('a cancelled request is reported as such, not as a failure', async () => {
  const { upload, chunks } = makeUpload(['cancelled']);
  await upload.start();
  assert.match(upload.error ?? '', /cancelled/i);
  assert.deepEqual(chunks, []);
});

await check('a silence ends the upload instead of hanging', async () => {
  const { upload, chunks } = makeUpload([]);
  upload.consentPolls = 3;
  await upload.start();
  assert.equal(upload.status, 'failed');
  assert.match(upload.error, /answer/i);
  assert.deepEqual(chunks, []);
});

await check('the client waits longer than the server does', async () => {
  // The server gives the recipient 120s and then declines with "No answer".
  // If the client gave up first it would invent its own verdict for a request
  // the server might still accept.
  const { upload } = makeUpload([]);
  assert.ok(upload.consentPolls * 1 >= 120, `${upload.consentPolls} polls of 1s is too few`);
});

await check('an already-accepted transfer never waits at all', async () => {
  // A file you send to your own PC comes back `pending`: the server accepted it
  // on creation, because the only device that could answer was the sender.
  const { upload, chunks, waits } = makeUpload([]);
  upload.deps.createTransfer = async () => ({
    transfer_id: 't2',
    chunk_size: 1000,
    total_chunks: 1,
    status: 'pending',
  });
  upload.deps.getTransfer = async () => ({ status: 'pending', received_chunks: [] });
  await upload.start();
  assert.equal(upload.status, 'completed');
  assert.deepEqual(chunks, [0]);
  assert.deepEqual(waits, [], 'it must not poll for a decision that was already made');
});

for (const [ok, name] of results) {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}`);
}
process.exit(results.every(([ok]) => ok) ? 0 : 1);
