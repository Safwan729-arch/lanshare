/**
 * Chunked upload with resume and cancel.
 *
 * One file = one transfer. Several files queue up and run one at a time, which
 * keeps a phone's upstream from being split N ways and makes progress honest.
 */

import {
  cancelTransfer,
  completeTransfer,
  createTransfer,
  digestOf,
  getTransfer,
  uploadChunk,
} from './api.js?v=13';

const MAX_CHUNK_ATTEMPTS = 3;
const RETRY_BASE_MS = 600;

/**
 * Largest file we are willing to hold in memory as a recovery measure.
 *
 * Only reached after a chunk has already stalled, so the ordinary path never
 * pays for it. A phone tab has perhaps a gigabyte before iOS kills it, and
 * losing the tab would be worse than failing the transfer.
 */
const BUFFER_FALLBACK_LIMIT = 256 * 1024 * 1024;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class Upload {
  constructor(file, receiverId, callbacks = {}) {
    this.file = file;
    this.receiverId = receiverId;
    this.callbacks = callbacks;

    this.transferId = null;
    this.chunkSize = 0;
    this.totalChunks = 0;
    this.sentChunks = new Set();
    this.bytesSent = 0;
    this.status = 'queued';
    this.error = null;
    this.current = null; // the in-flight chunk request
    this.cancelled = false;
    this.buffer = null; // whole-file bytes, only once slicing has failed us
  }

  get progress() {
    if (!this.file.size) return this.status === 'completed' ? 1 : 0;
    return Math.min(this.bytesSent / this.file.size, 1);
  }

  report() {
    if (this.callbacks.onChange) this.callbacks.onChange(this);
  }

  setStatus(status, error = null) {
    this.status = status;
    this.error = error;
    this.report();
  }

  async start() {
    try {
      this.setStatus('starting');
      // Null unless this browser is in a secure context and the file is small
      // enough to hold. When it is a hash, the server refuses the transfer if
      // what it assembled is not what we sent.
      const sha256 = await digestOf(this.file);
      const created = await createTransfer({
        filename: this.file.name,
        size: this.file.size,
        mimeType: this.file.type,
        receiverId: this.receiverId,
        sha256,
      });
      this.transferId = created.transfer_id;
      this.chunkSize = created.chunk_size;
      this.totalChunks = created.total_chunks;

      this.setStatus('uploading');
      await this.sendMissingChunks();

      if (this.cancelled) return;
      const finished = await completeTransfer(this.transferId);
      this.bytesSent = this.file.size;
      this.sha256 = finished.sha256;
      this.setStatus('completed');
    } catch (error) {
      if (this.cancelled || error.name === 'AbortError') {
        this.setStatus('cancelled');
      } else {
        this.setStatus('failed', error.message);
      }
    }
  }

  /** Ask the server what it already has, then send only the gaps. */
  async sendMissingChunks() {
    const status = await getTransfer(this.transferId);
    this.sentChunks = new Set(status.received_chunks);
    this.recalculateBytes();
    this.report();

    for (let index = 0; index < this.totalChunks; index += 1) {
      if (this.cancelled) return;
      if (this.sentChunks.has(index)) continue;
      await this.sendChunk(index);
    }
  }

  /**
   * The bytes for one chunk.
   *
   * Normally a lazy slice of the file, which costs nothing. Once `bufferFile`
   * has run we slice the copy in memory instead.
   */
  bodyFor(index) {
    const start = index * this.chunkSize;
    const end = Math.min(start + this.chunkSize, this.file.size);
    return this.buffer ? this.buffer.slice(start, end) : this.file.slice(start, end);
  }

  /**
   * Read the whole file once, so no further slices of it are needed.
   *
   * On iOS a file picked from the Photos library is exported lazily, and a
   * slice past the part already exported can simply never yield data: the
   * request is never dispatched, the server sees nothing at all, and the
   * upload sits at one chunk forever. Reading the file in a single pass
   * sidesteps the repeated reads.
   *
   * Returns false when the file is too large to hold, leaving the stall to be
   * reported honestly rather than trading it for a killed tab.
   */
  async bufferFile() {
    if (this.buffer || this.file.size > BUFFER_FALLBACK_LIMIT) return false;
    this.buffer = await this.file.arrayBuffer();
    return true;
  }

  async sendChunk(index) {
    const completedBytes = this.sentChunks.size * this.chunkSize;

    for (let attempt = 1; attempt <= MAX_CHUNK_ATTEMPTS; attempt += 1) {
      if (this.cancelled) return;
      // Rebuilt every attempt: a retry after a stall may need to come from the
      // buffered copy rather than from another slice of the file.
      this.current = uploadChunk({
        transferId: this.transferId,
        index,
        blob: this.bodyFor(index),
        // The suspect variable: a chunk that is the entire file has always
        // worked; one that is a slice of it has always hung.
        whole: this.chunkSize >= this.file.size ? 'yes' : 'no',
        onProgress: (loaded) => {
          this.bytesSent = Math.min(completedBytes + loaded, this.file.size);
          this.report();
        },
      });

      try {
        await this.current.promise;
        this.sentChunks.add(index);
        this.recalculateBytes();
        this.report();
        return;
      } catch (error) {
        if (this.cancelled || error.name === 'AbortError') throw error;

        // Nothing moved at all. Before spending another attempt on the same
        // dead read, try to get the bytes a different way.
        if (error.name === 'StallError' && !this.buffer) {
          try {
            await this.bufferFile();
          } catch {
            // The whole-file read failed too; the retry below will report it.
          }
        }

        if (attempt === MAX_CHUNK_ATTEMPTS) throw error;
        // A dropped Wi-Fi packet or a backgrounded tab: wait, then retry the
        // same index. Re-sending a chunk is safe, the server overwrites it.
        await sleep(RETRY_BASE_MS * 2 ** (attempt - 1));
      } finally {
        this.current = null;
      }
    }
  }

  recalculateBytes() {
    this.bytesSent = Math.min(this.sentChunks.size * this.chunkSize, this.file.size);
  }

  async cancel() {
    this.cancelled = true;
    if (this.current) this.current.abort();
    if (this.transferId && this.status !== 'completed') {
      try {
        await cancelTransfer(this.transferId);
      } catch {
        // Already gone server-side; nothing to clean up.
      }
    }
    this.setStatus('cancelled');
  }
}

/** Runs uploads one after another and keeps the UI informed. */
export class UploadQueue {
  constructor(onChange) {
    this.uploads = [];
    this.onChange = onChange;
    this.running = false;
  }

  add(files, receiverId) {
    const added = [...files].map(
      (file) => new Upload(file, receiverId, { onChange: this.onChange })
    );
    this.uploads.push(...added);
    this.onChange();
    this.run();
    return added;
  }

  async run() {
    if (this.running) return;
    this.running = true;
    try {
      for (const upload of this.uploads) {
        if (upload.status === 'queued') await upload.start();
      }
    } finally {
      this.running = false;
    }
  }

  active() {
    return this.uploads.filter(
      (upload) => !['completed', 'cancelled', 'failed'].includes(upload.status)
    );
  }

  clearFinished() {
    this.uploads = this.active();
    this.onChange();
  }
}
