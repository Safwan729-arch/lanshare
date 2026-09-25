/**
 * Every call to the server goes through this module.
 *
 * Note: the app is served over plain http on a LAN address, which is not a
 * "secure context", so crypto.randomUUID() and crypto.subtle are unavailable.
 * getRandomValues() is, so we build the UUID ourselves.
 */

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

async function request(path, options = {}) {
  const response = await fetch(path, options);
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
      user_agent: navigator.userAgent.slice(0, 512),
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

/**
 * Upload one chunk.
 *
 * XMLHttpRequest rather than fetch: fetch still has no upload progress events,
 * and a 4 MiB chunk on a phone is slow enough that the user needs to see it move.
 *
 * Returns an object with a promise and an abort() so a cancel can stop it.
 */
export function uploadChunk({ transferId, index, blob, onProgress }) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    xhr.open('PUT', `/api/transfers/${transferId}/chunks/${index}`, true);
    xhr.setRequestHeader('X-Device-Id', getDeviceId());
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    const token = getToken();
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

    if (onProgress) {
      xhr.upload.addEventListener('progress', (progressEvent) => {
        if (progressEvent.lengthComputable) onProgress(progressEvent.loaded);
      });
    }

    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText));
      } else {
        let detail = `Chunk ${index} failed (${xhr.status})`;
        try {
          detail = JSON.parse(xhr.responseText).detail || detail;
        } catch {
          // Keep the generic message.
        }
        reject(new Error(detail));
      }
    });
    xhr.addEventListener('error', () => reject(new Error('Network error')));
    xhr.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));

    xhr.send(blob);
  });

  return { promise, abort: () => xhr.abort() };
}
