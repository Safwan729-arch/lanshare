/**
 * Entry point: wires the registry, the socket, the upload queue and the DOM.
 */

import {
  AuthError,
  decideTrust,
  downloadFile,
  downloadUrl,
  getDeviceId,
  getOwnDevice,
  getServerInfo,
  getStoredName,
  getToken,
  listPeers,
  listPendingDevices,
  clearHistory,
  listTransfers,
} from './api.js?v=12';
import { DeviceRegistry, ensureRegistered, rename } from './devices.js?v=12';
import { UploadQueue } from './upload.js?v=12';
import { RealtimeConnection } from './ws.js?v=12';
import * as ui from './ui.js?v=12';
import { start as startParticles } from './particles.js?v=12';

const elements = ui.cacheElements();
const selfId = getDeviceId();

const incoming = new Map();
const registry = new DeviceRegistry(() => ui.renderDevices(registry, (id) => registry.select(id)));
const queue = new UploadQueue(() => ui.renderUploads(queue, (upload) => upload.cancel()));
const connection = new RealtimeConnection(selfId, getToken);

// -- realtime events ---------------------------------------------------------

connection.on('connection', ({ state }) => ui.setConnectionState(state));

connection.on('unregistered', async () => {
  // The server restarted with a fresh database: register again, then reconnect.
  const { trustState } = await ensureRegistered();
  if (trustState === 'trusted') {
    connection.reconnectNow();
  } else {
    awaitApproval();
  }
});

connection.on('unapproved', () => awaitApproval());

connection.on('device.list', ({ devices }) => registry.replace(devices));

connection.on('device.pending', () => {
  refreshPending();
  ui.toast('A device is asking to connect');
});

connection.on('device.pending.list', ({ devices }) => {
  ui.renderPending(devices, decide);
});

connection.on('device.approved', () => {
  // We were waiting; we are now allowed. Bring the page to life.
  ui.setAwaitingApproval(false);
  ui.toast('This device was approved', 'success');
  start();
});

connection.on('device.denied', () => {
  ui.setAwaitingApproval(true, { denied: true });
});

connection.on('device.joined', ({ device }) => ui.toast(`${device.name} joined`));

connection.on('transfer.incoming', (data) => {
  incoming.set(data.transfer_id, {
    transferId: data.transfer_id,
    filename: data.filename,
    size: data.size,
    senderName: data.sender_name,
    received: 0,
    ready: false,
  });
  renderIncoming();
  ui.toast(`${data.sender_name} is sending ${data.filename}`);
});

connection.on('transfer.progress', (data) => {
  const item = incoming.get(data.transfer_id);
  if (!item) return;
  item.received = data.bytes_received;
  renderIncoming();
});

connection.on('transfer.completed', (data) => {
  const item = incoming.get(data.transfer_id);
  if (item) {
    item.ready = true;
    item.downloadUrl = downloadUrl(data.transfer_id);
    renderIncoming();
    ui.toast(`${item.filename} is ready to save`, 'success');
  }
  refreshHistory();
});

connection.on('transfer.failed', (data) => {
  incoming.delete(data.transfer_id);
  renderIncoming();
  ui.toast(`Transfer failed: ${data.error}`, 'error');
});

connection.on('transfer.cancelled', (data) => {
  incoming.delete(data.transfer_id);
  renderIncoming();
});

async function saveFile(transferId, filename) {
  try {
    await downloadFile(transferId, filename);
  } catch (error) {
    ui.toast(`Could not save the file: ${error.message}`, 'error');
  }
}

function renderIncoming() {
  ui.renderIncoming(
    [...incoming.values()],
    (transferId) => {
      incoming.delete(transferId);
      renderIncoming();
    },
    (item) => saveFile(item.transferId, item.filename)
  );
}

// -- sending -----------------------------------------------------------------

function sendFiles(files) {
  if (!files || files.length === 0) return;
  if (!registry.selected) {
    ui.toast('Pick a device to send to first', 'error');
    return;
  }
  queue.add(files, registry.selected.id);
}

elements.dropzone.addEventListener('click', () => elements['file-input'].click());

elements.dropzone.addEventListener('keydown', (keyEvent) => {
  if (keyEvent.key === 'Enter' || keyEvent.key === ' ') {
    keyEvent.preventDefault();
    elements['file-input'].click();
  }
});

elements['file-input'].addEventListener('change', (changeEvent) => {
  sendFiles(changeEvent.target.files);
  changeEvent.target.value = ''; // so picking the same file twice still fires
});

for (const type of ['dragenter', 'dragover']) {
  elements.dropzone.addEventListener(type, (dragEvent) => {
    dragEvent.preventDefault();
    elements.dropzone.dataset.dragging = 'true';
  });
}

for (const type of ['dragleave', 'drop']) {
  elements.dropzone.addEventListener(type, (dragEvent) => {
    dragEvent.preventDefault();
    elements.dropzone.dataset.dragging = 'false';
  });
}

elements.dropzone.addEventListener('drop', (dropEvent) => {
  sendFiles(dropEvent.dataTransfer.files);
});

// Decoration, so it must never be able to break the app it decorates.
const particleField = document.getElementById('particle-field');
if (particleField) {
  try {
    startParticles(particleField);
  } catch {
    // A browser without ResizeObserver or canvas still gets a working app.
  }
}

elements['clear-finished'].addEventListener('click', () => queue.clearFinished());

// Confirmed because it cannot be undone, and because the entries disappear
// from the other device's history too - worth knowing before you agree.
elements['clear-history'].addEventListener('click', async () => {
  if (!confirm('Clear the transfer history? Files already received are kept.')) return;
  try {
    await clearHistory();
  } catch (error) {
    ui.toast(`Could not clear the history: ${error.message}`, 'error');
  }
  await refreshHistory();
});

// -- identity ----------------------------------------------------------------

elements['self-name'].parentElement.addEventListener('click', async () => {
  const next = prompt('Name this device', getStoredName());
  if (next === null) return;
  const saved = await rename(next, connection);
  ui.setSelfName(saved);
});

// -- history -----------------------------------------------------------------

async function refreshHistory() {
  try {
    const response = await listTransfers(25);
    ui.renderHistory(response.transfers, selfId, (transfer) =>
      saveFile(transfer.id, transfer.filename)
    );
  } catch {
    // History is a nicety; a failure here should not break sending.
  }
}

// -- pairing -----------------------------------------------------------------

// How often an unapproved device asks whether it has been allowed. It cannot
// hold a WebSocket, so there is no event to wait for.
const APPROVAL_POLL_MS = 2500;
let approvalTimer = null;

async function decide(deviceId, decision) {
  try {
    await decideTrust(deviceId, decision);
    await refreshPending();
    ui.toast(decision === 'approve' ? 'Device allowed' : 'Device denied');
  } catch (error) {
    ui.toast(`Could not update that device: ${error.message}`, 'error');
  }
}

async function refreshPending() {
  try {
    const response = await listPendingDevices();
    ui.renderPending(response.devices, decide);
  } catch {
    // Only trusted devices may look; ignore otherwise.
  }
}

/** Show the waiting screen and poll until the host decides. */
function awaitApproval() {
  ui.setAwaitingApproval(true, { name: getStoredName() });
  connection.close();
  if (approvalTimer) return;

  approvalTimer = setInterval(async () => {
    try {
      const device = await getOwnDevice();
      if (device.trust_state === 'trusted') {
        clearInterval(approvalTimer);
        approvalTimer = null;
        ui.setAwaitingApproval(false);
        ui.toast('This device was approved', 'success');
        start();
      } else if (device.trust_state === 'blocked') {
        ui.setAwaitingApproval(true, { denied: true });
      } else {
        // Still waiting, but we did reach the server - clear any stale warning.
        ui.setAwaitingApproval(true, { name: getStoredName() });
      }
    } catch (error) {
      // A denied device is deleted outright, so its token stops working.
      if (error instanceof AuthError) {
        ui.setAwaitingApproval(true, { denied: true });
      } else {
        // The server went away mid-wait - a restart, a sleep, Wi-Fi dropping.
        // Without this the spinner is indistinguishable from patience.
        ui.setAwaitingApproval(true, { offline: true, name: getStoredName() });
      }
    }
  }, APPROVAL_POLL_MS);
}

// -- peers -------------------------------------------------------------------

// Peers arrive by UDP broadcast on the server side, so there is no event to
// push to the browser - poll, at a fraction of the announce interval.
const PEER_POLL_MS = 15000;

async function refreshPeers() {
  try {
    const response = await listPeers();
    ui.renderPeers(response.peers);
  } catch {
    // Discovery is a nicety; never let it break the page.
  }
}

// -- lifecycle ---------------------------------------------------------------

document.addEventListener('visibilitychange', () => {
  // iOS Safari kills the socket while backgrounded. Reconnect the moment the
  // user comes back rather than waiting out the backoff.
  if (document.visibilityState === 'visible') {
    connection.reconnectNow();
    registry.refresh().catch(() => {});
    refreshHistory();
    refreshPeers();
  }
});

let pollingStarted = false;

async function start() {
  ui.setConnectionState('offline');
  try {
    const { name, trustState } = await ensureRegistered();
    ui.setSelfName(name);

    if (trustState !== 'trusted') {
      awaitApproval();
      return;
    }

    ui.setAwaitingApproval(false);

    const info = await getServerInfo();
    ui.setServerHint(info);
    ui.setPairing(info);

    await registry.refresh();
    await refreshHistory();
    await refreshPending();
    connection.connect();

    await refreshPeers();
    if (!pollingStarted) {
      pollingStarted = true;
      setInterval(refreshPeers, PEER_POLL_MS);
    }
  } catch (error) {
    ui.toast(`Could not reach the server: ${error.message}`, 'error');
  }
}

start();
