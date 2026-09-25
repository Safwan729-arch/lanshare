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
  getTransfer,
  uploadChunk,
} from './api.js';

const MAX_CHUNK_ATTEMPTS = 3;
const RETRY_BASE_MS = 600;

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
      const created = await createTransfer({
        filename: this.file.name,
        size: this.file.size,
        mimeType: this.file.type,
        receiverId: this.receiverId,
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

  async sendChunk(index) {
    const start = index * this.chunkSize;
    const blob = this.file.slice(start, Math.min(start + this.chunkSize, this.file.size));
    const completedBytes = this.sentChunks.size * this.chunkSize;

    for (let attempt = 1; attempt <= MAX_CHUNK_ATTEMPTS; attempt += 1) {
      if (this.cancelled) return;
      this.current = uploadChunk({
        transferId: this.transferId,
        index,
        blob,
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
