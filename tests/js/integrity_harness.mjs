/**
 * Behavioural tests for the pre-upload hash.
 *
 * Why this exists: the check is an *extra*, and an extra that can break the
 * thing it guards is worse than no extra at all. Two ways it could:
 *
 *  - by reading a whole file into memory, which is exactly what the upload path
 *    spends its complexity avoiding (ADR-0009: a whole-file read is a recovery
 *    measure, not the happy path, because it is what gets an iOS tab killed);
 *  - by throwing, on a browser without `crypto.subtle` - which is every phone
 *    here, since plain http is not a secure context.
 *
 * Invoked by tests/test_upload_stall.py with the module URL as argv[2].
 */
import assert from 'node:assert/strict';
import { createHash, webcrypto } from 'node:crypto';

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

// node defines globalThis.crypto with a getter, so it cannot simply be assigned.
function setCrypto(value) {
  Object.defineProperty(globalThis, 'crypto', { value, configurable: true, writable: true });
}

/** A file that counts how often anything reads all of it. */
class FakeFile {
  constructor(size, byte = 0x61) {
    this.size = size;
    this.name = 'thing.bin';
    this.type = 'application/octet-stream';
    this.bytes = new Uint8Array(size).fill(byte);
    this.wholeReads = 0;
  }

  async arrayBuffer() {
    this.wholeReads += 1;
    return this.bytes.buffer;
  }
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

await check('a small file is hashed, and the hash is the real SHA-256', async () => {
  setCrypto(webcrypto);
  const file = new FakeFile(1024);
  const digest = await api.digestOf(file);
  const expected = createHash('sha256').update(Buffer.from(file.bytes)).digest('hex');
  assert.equal(digest, expected);
  assert.equal(file.wholeReads, 1);
});

await check('a file past the limit is never read', async () => {
  setCrypto(webcrypto);
  const file = new FakeFile(api.INTEGRITY_LIMIT + 1);
  assert.equal(await api.digestOf(file), null);
  assert.equal(file.wholeReads, 0, 'hashing must not buffer a large file');
});

await check('a browser without crypto.subtle gets null, not an exception', async () => {
  setCrypto(undefined);
  const file = new FakeFile(1024);
  assert.equal(await api.digestOf(file), null);
  assert.equal(file.wholeReads, 0);
});

await check('a digest that throws is not allowed to fail the upload', async () => {
  setCrypto({
    subtle: {
      digest() {
        throw new Error('no');
      },
    },
  });
  assert.equal(await api.digestOf(new FakeFile(1024)), null);
});

await check('the limit stays well under what the upload path treats as safe', () => {
  // ADR-0009's own fallback limit is 256 MiB and is only reached after a stall.
  // This one is on the happy path, so it has to be far smaller.
  assert.ok(api.INTEGRITY_LIMIT <= 8 * 1024 * 1024);
});

for (const [ok, name] of results) {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}`);
}
process.exit(results.every(([ok]) => ok) ? 0 : 1);
