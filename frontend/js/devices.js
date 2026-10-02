/**
 * This browser's identity, plus the list of devices we can send to.
 *
 * The device list is authoritative from the server: it arrives on the
 * `device.list` WebSocket event and on the REST call used at startup.
 */

import {
  AuthError,
  getDeviceId,
  getStoredName,
  guessDeviceName,
  listDevices,
  registerDevice,
  resetIdentity,
  setStoredName,
  setToken,
} from './api.js?v=13';

export class DeviceRegistry {
  constructor(onChange) {
    this.devices = [];
    this.onChange = onChange;
    this.selectedId = null;
  }

  get selfId() {
    return getDeviceId();
  }

  /** Everyone except this browser - you cannot send a file to yourself. */
  get targets() {
    return this.devices.filter((device) => device.id !== this.selfId);
  }

  get selected() {
    return this.targets.find((device) => device.id === this.selectedId) || null;
  }

  replace(devices) {
    this.devices = devices;
    // If the selected device disappeared, fall back to the first one left.
    if (!this.selected) {
      this.selectedId = this.targets.length ? this.targets[0].id : null;
    }
    this.onChange();
  }

  select(deviceId) {
    this.selectedId = deviceId;
    this.onChange();
  }

  async refresh() {
    const response = await listDevices();
    this.replace(response.devices);
  }
}

/**
 * Register this browser and return { name, trustState }.
 *
 * A new device is issued a token here — the only time the plaintext exists
 * outside this browser. If the server rejects our id (it exists but we cannot
 * prove it is ours), the only way forward is to become a new device.
 */
export async function ensureRegistered() {
  const name = getStoredName() || guessDeviceName();
  setStoredName(name);

  let response;
  try {
    response = await registerDevice(name);
  } catch (error) {
    if (!(error instanceof AuthError)) throw error;
    resetIdentity();
    response = await registerDevice(name);
  }

  if (response.token) setToken(response.token);
  return { name: response.device.name, trustState: response.device.trust_state };
}

export async function rename(name, connection) {
  const trimmed = name.trim().slice(0, 64);
  if (!trimmed) return getStoredName();
  setStoredName(trimmed);
  // Over the socket so other devices see it immediately; the REST register
  // call is the fallback for when the socket happens to be down.
  if (!connection.send('device.rename', { name: trimmed })) {
    await registerDevice(trimmed);
  }
  return trimmed;
}
