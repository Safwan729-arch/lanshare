/**
 * Every call to the server goes through this module.
 *
 * Note: the app is served over plain http on a LAN address, which is not a
 * "secure context", so crypto.randomUUID() and crypto.subtle are unavailable.
 * getRandomValues() is, so we build the UUID ourselves.
 */

/**
 * Bumped whenever the client changes in a way worth telling the server about.
 * It rides along in the registered user agent, so `devices.user_agent` shows
 * which build a phone is actually running. Without it there is no way to tell
 * a stale cached page from a current one, and a browser cache turned a fixed
 * bug into a bug that looked unfixed.
 */
const CLIENT_VERSION = '11';

const DEVICE_ID_KEY = 'lanshare.device_id';
const DEVICE_NAME_KEY = 'lanshare.device_name';
const DEVICE_TOKEN_KEY = 'lanshare.device_token';

function randomUuid() {
  const bytes = new Uint8Array(16);
  if (globalThis.crypto && globalThis.crypto.getRandomValues) {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 1
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** This browser's stable id. Created once, then kept in localStorage. */
export function getDeviceId() {
  let id = localStorage.getItem(DEVICE_ID_KEY);
  if (!id) {
    id = randomUuid();
    localStorage.setItem(DEVICE_ID_KEY, id);
  }
  return id;
}

/** The device credential, issued once at registration. */
export function getToken() {
  return localStorage.getItem(DEVICE_TOKEN_KEY) || '';
}

export function setToken(token) {
  localStorage.setItem(DEVICE_TOKEN_KEY, token);
}

/**
 * Forget this device entirely and start over with a fresh id.
 *
 * Used when the server rejects our id — it exists but we cannot prove it is
 * ours, so the only way forward is to become a new device.
 */
export function resetIdentity() {
  localStorage.removeItem(DEVICE_ID_KEY);
  localStorage.removeItem(DEVICE_TOKEN_KEY);
  return getDeviceId();
}

export function getStoredName() {
  return localStorage.getItem(DEVICE_NAME_KEY) || '';
}

export function setStoredName(name) {
  localStorage.setItem(DEVICE_NAME_KEY, name);
}

/** A friendly default like "iPhone - Safari", guessed from the user agent. */
export function guessDeviceName() {
  const ua = navigator.userAgent;
  const platform =
    (/iPhone/.test(ua) && 'iPhone') ||
    (/iPad/.test(ua) && 'iPad') ||
    (/Android/.test(ua) && 'Android') ||
    (/Macintosh/.test(ua) && 'Mac') ||
    (/Windows/.test(ua) && 'Windows') ||
    (/Linux/.test(ua) && 'Linux') ||
    'Device';

  // Order matters: Edge and Chrome both claim to be Safari.
  const browser =
    (/Edg\//.test(ua) && 'Edge') ||
    (/OPR\//.test(ua) && 'Opera') ||
    (/Firefox\//.test(ua) && 'Firefox') ||
    (/Chrome\//.test(ua) && 'Chrome') ||
    (/Safari\//.test(ua) && 'Safari') ||
    'Browser';

  return `${platform} - ${browser}`;
}

function deviceHeaders(extra = {}) {
  const headers = { 'X-Device-Id': getDeviceId(), ...extra };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

/** Thrown for 401/403 so callers can tell "not allowed" from "broken". */
export class AuthError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'AuthError';
    this.status = status;
  }
}

/**
 * How long to wait for the server before giving up on a plain API call.
 *
 * Generous: this is a LAN, so anything healthy answers in milliseconds. The
 * number exists to bound a request that will *never* answer, not to police a
 * slow one.
 */
export const REQUEST_TIMEOUT_MS = 15000;

/**
 * Every call to the server, with a bound on how long it may hang.
 *
 * Without this a request can wait forever. That is not hypothetical: the server
 * closes an idle keep-alive connection, the browser does not notice and reuses
 * it, and a `POST` on the dead socket is never retried - a POST is not safe to
 * repeat, so the browser will not quietly re-send it the way it would a GET.
 * The upload then sat on "Starting..." with no error, and the whole queue
 * stalled behind it showing "Waiting...".
 *
 * `AbortController` rather than `AbortSignal.timeout` so the timer is an
 * ordinary `setTimeout`, which the tests can drive with a clock they control.
 */
async function request(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? REQUEST_TIMEOUT_MS);

  let response;
  try {
    response = await fetch(path, { ...options, signal: controller.signal });
  } catch (error) {
    // Say what the user can do about it. "Failed to fetch" tells them nothing.
    if (controller.signal.aborted) {
      throw new Error('The server did not respond. Check you are on the same Wi-Fi and try again.');
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body.detail) detail = typeof body.detail === 'string' ? body.detail : detail;
    } catch {
      // Not JSON. The status line is all we have.
    }
    if (response.status === 401 || response.status === 403) {
      throw new AuthError(detail, response.status);
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

export function getServerInfo() {
  // Trusted-only since the pairing review: it carries the host device id, the
  // LAN URL and the QR. Every call from this module sends the credential.
  return request('/api/server-info', { headers: deviceHeaders() });
}

export function registerDevice(name) {
  return request('/api/devices/register', {
    method: 'POST',
    // Send the token if we have one: refreshing an existing registration
    // requires proving the device id is really ours.
    headers: deviceHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      device_id: getDeviceId(),
      name,
      user_agent: `${navigator.userAgent} lanshare-client/${CLIENT_VERSION}`.slice(0, 512),
    }),
  });
}

export function listDevices() {
  return request('/api/devices', { headers: deviceHeaders() });
}

/** This device's own record — the one thing a pending device may ask for. */
export function getOwnDevice() {
  return request('/api/devices/me', { headers: deviceHeaders() });
}

export function listPendingDevices() {
  return request('/api/devices/pending', { headers: deviceHeaders() });
}

export function decideTrust(deviceId, decision) {
  return request(`/api/devices/${deviceId}/trust`, {
    method: 'POST',
    headers: deviceHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ decision }),
  });
}

export function createTransfer({ filename, size, mimeType, receiverId }) {
  return request('/api/transfers', {
    method: 'POST',
    headers: deviceHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      filename,
      size,
      mime_type: mimeType || null,
      receiver_id: receiverId,
    }),
  });
}

export function getTransfer(transferId) {
  return request(`/api/transfers/${transferId}`, { headers: deviceHeaders() });
}

export function completeTransfer(transferId) {
  return request(`/api/transfers/${transferId}/complete`, {
    method: 'POST',
    headers: deviceHeaders(),
  });
}

export function cancelTransfer(transferId) {
  return request(`/api/transfers/${transferId}`, {
    method: 'DELETE',
    headers: deviceHeaders(),
  });
}

export function listTransfers(limit = 25) {
  // No device_id parameter: the server scopes history to the caller.
  return request(`/api/transfers?limit=${limit}`, { headers: deviceHeaders() });
}

/**
 * Forget this device's finished transfers.
 *
 * The server scopes this to the caller, so there is nothing to pass. Note the
 * row is shared with the other device in each transfer, which loses those
 * entries from its history too.
 */
export function clearHistory() {
  return request('/api/transfers', {
    method: 'DELETE',
    headers: deviceHeaders(),
  });
}

export function listPeers() {
  return request('/api/peers', { headers: deviceHeaders() });
}

export function downloadUrl(transferId) {
  return `/api/files/${transferId}/download`;
}

/**
 * Fetch a completed file and hand it to the browser as a download.
 *
 * A plain <a href> cannot be used any more: the endpoint needs an
 * Authorization header, and links cannot carry one. So we fetch the bytes
 * ourselves and save them from a blob URL instead.
 */
export async function downloadFile(transferId, filename) {
  const response = await fetch(downloadUrl(transferId), { headers: deviceHeaders() });
  if (!response.ok) {
    throw new Error(`Download failed (${response.status})`);
  }

  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement('a');
    link.href = url;
    link.download = filename || 'download';
    document.body.append(link);
    link.click();
    link.remove();
  } finally {
    // Give the browser a moment to start the save before revoking.
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
}

/** How long an upload that has started may stall before it is declared dead. */
export const CHUNK_STALL_MS = 30000;

/**
 * How long a chunk may sit without sending a single byte.
 *
 * Much shorter than `CHUNK_STALL_MS`, because the two situations are not alike.
 * An upload that has moved bytes and then pauses may simply be on a bad link and
 * deserves patience. One that has not moved *any* byte on a LAN is not slow, it
 * is broken - on iOS that is a slice the browser will never manage to read, and
 * waiting thirty seconds to find out only delays the recovery.
 */
export const CHUNK_FIRST_BYTE_MS = 10000;

/** How often to look for a response that has fully arrived but not finished. */
const COMPLETE_POLL_MS = 50;

/**
 * Upload one chunk.
 *
 * XMLHttpRequest rather than fetch: fetch still has no upload progress events,
 * and a 4 MiB chunk on a phone is slow enough that the user needs to see it move.
 *
 * A stalled connection fires no events whatsoever - not `error`, not `timeout`,
 * nothing - so without a watchdog the promise simply never settles. That is not
 * hypothetical: a 47 MiB video from an iPhone stopped one chunk in and the bar
 * sat at 8% indefinitely, because the retry loop in upload.js only ever sees an
 * `error` event. A timer is the only thing that can notice silence.
 *
 * `xhr.timeout` is deliberately not used: it caps the *whole* request, which
 * would kill a 64 MiB chunk that is uploading perfectly well on a slow link.
 * What matters is time since the last byte moved, so the watchdog is re-armed
 * by every sign of life.
 *
 * Returns an object with a promise and an abort() so a cancel can stop it.
 */
export function uploadChunk({
  transferId,
  index,
  blob,
  onProgress,
  whole = '?',
  stallMs = CHUNK_STALL_MS,
  firstByteMs = CHUNK_FIRST_BYTE_MS,
}) {
  const xhr = new XMLHttpRequest();
  let stalled = false;
  let delivered = false;
  let poll = null;
  let started = false;
  let watchdog = null;

  // -- diagnostics ---------------------------------------------------------
  // The server can prove it answered in milliseconds; only the browser knows
  // whether the answer ever arrived. This records what this end saw and posts
  // it on a separate connection, which is the one thing the stuck one cannot do.
  const traceStart = Date.now();
  const trace = [];
  // Whether the body is a file-backed Blob or plain memory, and whether it
  // spans the whole file or only part of it. Every upload that has ever
  // succeeded from the phone sent a whole file; every one that hung sent a
  // piece. This records which, so that stops being an inference.
  const bodyKind = blob instanceof Blob ? 'blob' : 'buffer';
  let progressCount = 0;
  const note = (name, detail) => {
    trace.push({ t: Date.now() - traceStart, name, detail: String(detail) });
  };
  // Both guarded: reading either before the headers arrive throws in some browsers.
  const header = (name) => {
    try {
      return xhr.getResponseHeader(name) ?? '-';
    } catch {
      return '?';
    }
  };
  const bodyLength = () => {
    try {
      return (xhr.responseText || '').length;
    } catch {
      return '?';
    }
  };
  const reportTrace = (outcome) => {
    try {
      fetch('/api/diag/client', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        keepalive: true,
        body: JSON.stringify({
          label: 'uploadChunk', index, outcome,
          size: blob && (blob.size !== undefined ? blob.size : blob.byteLength),
          events: trace,
        }),
      }).catch(() => {});
    } catch {
      // A diagnostic must never be able to break the transfer.
    }
  };

  const promise = new Promise((resolve, reject) => {
    const disarm = () => {
      if (watchdog !== null) {
        clearTimeout(watchdog);
        watchdog = null;
      }
    };

    const arm = () => {
      disarm();
      const limit = started ? stallMs : firstByteMs;
      watchdog = setTimeout(() => {
        // Reject before aborting. The abort handler below would otherwise
        // label this AbortError, which upload.js treats as "the user cancelled"
        // and rethrows - turning a retryable stall into a dead transfer.
        stalled = true;
        const error = new Error(
          started
            ? `Chunk ${index} stalled - nothing sent for ${stallMs / 1000}s`
            : `Chunk ${index} stalled - not a single byte was sent in ${firstByteMs / 1000}s`
        );
        // Named so upload.js can tell a stall from an ordinary network error
        // and try reading the file a different way.
        error.name = 'StallError';
        note('watchdog.fired', `readyState=${xhr.readyState} status=${xhr.status}`);
        // The decisive measurement. The server can prove it sent a complete
        // Content-Length response; this says how much of it the phone holds.
        // got === want means every byte arrived and only the `load` event is
        // missing. got < want means the body genuinely stopped mid-flight.
        note('watchdog.body', `got=${bodyLength()} want=${header('Content-Length')}`);
        reportTrace('stalled');
        reject(error);
        xhr.abort();
      }, limit);
    };

    const stopPoll = () => {
      if (poll !== null) {
        clearTimeout(poll);
        poll = null;
      }
    };

    const settle = (fn) => (...args) => {
      disarm();
      stopPoll();
      fn(...args);
    };

    /** Hand the server's answer to the caller, however we came by it. */
    const deliver = (how) => {
      delivered = true;
      note(how, `status=${xhr.status} bytes=${bodyLength()}`);
      reportTrace(how);

      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch {
          reject(new Error(`Chunk ${index} returned a malformed response`));
        }
        return;
      }

      let detail = `Chunk ${index} failed (${xhr.status})`;
      try {
        detail = JSON.parse(xhr.responseText).detail || detail;
      } catch {
        // Keep the generic message.
      }
      reject(new Error(detail));
    };

    /**
     * Take the response as soon as all of it is here.
     *
     * iOS Safari delivers the entire body and then refuses to finish the
     * request: readyState stays at 3 and `load` never fires. Measured on the
     * device - the body was complete at 131ms and was still sitting there,
     * untouched, when the watchdog gave up 30 seconds later. Every multi-chunk
     * upload from an iPhone died waiting for an event that was never coming.
     *
     * Content-Length is what makes this safe: a short body is not acted on, so
     * a response that really is still arriving is left to the watchdog.
     */
    const checkComplete = () => {
      poll = null;
      if (xhr.readyState < 3) return schedulePoll();

      const want = Number(header('Content-Length'));
      const got = bodyLength();
      if (!Number.isFinite(want) || want <= 0 || typeof got !== 'number' || got < want) {
        return schedulePoll();
      }

      disarm();
      deliver('response.taken');
      // Safari allows a handful of connections per host, and this one is
      // never going to close by itself. Hand the slot back.
      xhr.abort();
      return undefined;
    };

    const schedulePoll = () => {
      poll = setTimeout(checkComplete, COMPLETE_POLL_MS);
    };

    xhr.open('PUT', `/api/transfers/${transferId}/chunks/${index}`, true);
    xhr.setRequestHeader('X-Device-Id', getDeviceId());
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    const token = getToken();
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

    // Registered unconditionally - the watchdog needs it even when the caller
    // does not want progress.
    xhr.upload.addEventListener('progress', (progressEvent) => {
      // The first byte moved: from here on the generous limit applies.
      started = true;
      progressCount += 1;
      if (progressCount <= 2 || progressEvent.loaded === progressEvent.total) {
        note('upload.progress', `${progressEvent.loaded}/${progressEvent.total}`);
      }
      arm();
      if (onProgress && progressEvent.lengthComputable) onProgress(progressEvent.loaded);
    });

    // The body is out. No further upload events will arrive, but the server
    // still has to write the chunk and answer, so keep watching.
    xhr.upload.addEventListener('load', () => {
      note('upload.load', 'body fully handed to the network');
      arm();
    });

    // The state machine is the whole point: readyState 2 means the response
    // headers arrived. If it never reaches 2, no answer ever came back.
    xhr.addEventListener('readystatechange', () => {
      note('readystatechange', `readyState=${xhr.readyState} status=${xhr.status}`);
      if (xhr.readyState === 2) {
        note('headers', `len=${header('Content-Length')} conn=${header('Connection')}`);
      } else if (xhr.readyState === 3) {
        note('loading', `got=${bodyLength()} want=${header('Content-Length')}`);
      }
    });

    xhr.addEventListener(
      'load',
      settle(() => deliver('load'))
    );
    xhr.addEventListener(
      'error',
      settle(() => {
        note('error', `readyState=${xhr.readyState} status=${xhr.status}`);
        reportTrace('error');
        reject(new Error('Network error'));
      })
    );
    xhr.addEventListener(
      'abort',
      settle(() => {
        // A request we already took the answer from is torn down on purpose.
        if (!stalled && !delivered) reject(new DOMException('Aborted', 'AbortError'));
      })
    );

    arm();
    note('send', `bytes=${blob && (blob.size !== undefined ? blob.size : blob.byteLength)}`);
    note('body', `kind=${bodyKind} whole=${whole}`);

    xhr.send(blob);
    schedulePoll();
  });

  return { promise, abort: () => xhr.abort() };
}
