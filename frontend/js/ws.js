/**
 * WebSocket client: one connection, auto-reconnect with exponential backoff.
 *
 * iOS Safari freezes JS when the tab goes to the background and kills the
 * socket, so reconnecting cleanly is the normal case here, not an edge case.
 */

const PING_INTERVAL_MS = 25000;
const FIRST_RETRY_MS = 500;
const MAX_RETRY_MS = 15000;

export class EventBus {
  constructor() {
    this.handlers = new Map();
  }

  on(type, handler) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type).add(handler);
    return () => this.handlers.get(type).delete(handler);
  }

  emit(type, data) {
    for (const handler of this.handlers.get(type) || []) handler(data);
    for (const handler of this.handlers.get('*') || []) handler({ type, data });
  }
}

export const SUBPROTOCOL = 'lanshare.v1';

export class RealtimeConnection extends EventBus {
  constructor(deviceId, getToken) {
    super();
    this.deviceId = deviceId;
    this.getToken = getToken;
    this.socket = null;
    this.retryDelay = FIRST_RETRY_MS;
    this.pingTimer = null;
    this.retryTimer = null;
    this.closedByUs = false;
  }

  get url() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${location.host}/ws?device_id=${encodeURIComponent(this.deviceId)}`;
  }

  connect() {
    this.closedByUs = false;
    clearTimeout(this.retryTimer);

    // The token rides in Sec-WebSocket-Protocol rather than the query
    // string, because uvicorn writes the full request line to its access
    // log and a token in the URL would be written to disk in plain text.
    const token = this.getToken ? this.getToken() : '';
    const protocols = token ? [SUBPROTOCOL, `token.${token}`] : [SUBPROTOCOL];
    this.socket = new WebSocket(this.url, protocols);

    this.socket.addEventListener('open', () => {
      this.retryDelay = FIRST_RETRY_MS;
      this.emit('connection', { state: 'online' });
      this.startPinging();
    });

    this.socket.addEventListener('message', (messageEvent) => {
      let payload;
      try {
        payload = JSON.parse(messageEvent.data);
      } catch {
        return; // Ignore anything that is not our envelope.
      }
      this.emit(payload.type, payload.data || {});
    });

    this.socket.addEventListener('close', (closeEvent) => {
      this.stopPinging();
      this.emit('connection', { state: 'offline', code: closeEvent.code });
      // 4404: the server does not know this device. 4403: known but not
      // approved. Neither is fixed by retrying the socket, so hand it back to
      // the caller instead of looping.
      const fatal = closeEvent.code === 4404 || closeEvent.code === 4403;
      if (!this.closedByUs && !fatal) this.scheduleReconnect();
      if (closeEvent.code === 4404) this.emit('unregistered', {});
      if (closeEvent.code === 4403) this.emit('unapproved', {});
    });

    this.socket.addEventListener('error', () => {
      // 'close' always follows, so reconnection is handled there.
    });
  }

  scheduleReconnect() {
    this.emit('connection', { state: 'reconnecting', delay: this.retryDelay });
    this.retryTimer = setTimeout(() => this.connect(), this.retryDelay);
    this.retryDelay = Math.min(this.retryDelay * 2, MAX_RETRY_MS);
  }

  startPinging() {
    this.stopPinging();
    this.pingTimer = setInterval(() => this.send('ping'), PING_INTERVAL_MS);
  }

  stopPinging() {
    clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  send(type, data = {}) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type, data }));
      return true;
    }
    return false;
  }

  /** Called when the tab comes back, to skip the remaining backoff wait. */
  reconnectNow() {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) return;
    clearTimeout(this.retryTimer);
    this.retryDelay = FIRST_RETRY_MS;
    this.connect();
  }

  close() {
    this.closedByUs = true;
    this.stopPinging();
    clearTimeout(this.retryTimer);
    if (this.socket) this.socket.close();
  }
}
