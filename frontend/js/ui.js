/**
 * All DOM work lives here.
 *
 * Device names and filenames come from other people's devices, so everything
 * user-supplied is set with textContent and never interpolated into HTML.
 */

const elements = {};

export function cacheElements() {
  const ids = [
    'status-dot',
    'status-text',
    'self-name',
    'device-list',
    'devices-empty',
    'dropzone',
    'file-input',
    'uploads-panel',
    'upload-list',
    'clear-finished',
    'incoming-panel',
    'incoming-list',
    'clear-history',
  'history-list',
    'history-empty',
    'toast',
    'server-hint',
    'qr-image',
    'pairing-url',
    'pairing-mdns',
    'pairing-mdns-url',
    'peers-panel',
    'peer-list',
    'waiting-screen',
    'waiting-name',
    'waiting-denied',
    'waiting-offline',
    'waiting-host-url',
    'pending-panel',
    'pending-list',
    'requests-panel',
    'request-list',
    'accept-all',
  ];
  for (const id of ids) {
    elements[id] = document.getElementById(id);
  }
  return elements;
}

export function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function formatTime(isoString) {
  if (!isoString) return '';
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function setConnectionState(state) {
  const labels = {
    online: 'Connected',
    offline: 'Disconnected',
    reconnecting: 'Reconnecting…',
  };
  elements['status-dot'].dataset.state = state;
  elements['status-text'].textContent = labels[state] || state;
}

export function setSelfName(name) {
  elements['self-name'].textContent = name;
}

export function setServerHint(info) {
  elements['server-hint'].textContent = `${info.server_name} · ${info.lan_url}`;
}

export function setPairing(info) {
  elements['pairing-url'].textContent = info.lan_url;
  // The SVG is server-generated, but routing it through an <img> data URI
  // rather than innerHTML keeps the "never inject markup" rule intact.
  // The data URI must be percent-encoded: the markup contains '#' in its
  // colours, which would otherwise be read as a fragment and cut it short.
  const encoded = encodeURIComponent(info.qr_svg).replace(/'/g, '%27');
  elements['qr-image'].src = `data:image/svg+xml;charset=utf-8,${encoded}`;

  // mdns_url is null when advertising failed, and Android browsers cannot
  // resolve .local anyway, so this is an extra - never the primary address.
  const hasMdns = Boolean(info.mdns_url);
  elements['pairing-mdns'].hidden = !hasMdns;
  if (hasMdns) elements['pairing-mdns-url'].textContent = info.mdns_url;
}

export function renderDevices(registry, onSelect) {
  const list = elements['device-list'];
  list.replaceChildren();

  const targets = registry.targets;
  elements['devices-empty'].hidden = targets.length > 0;

  for (const device of targets) {
    const button = element('button', 'device');
    button.type = 'button';
    button.dataset.selected = String(device.id === registry.selectedId);
    button.setAttribute('aria-pressed', String(device.id === registry.selectedId));

    button.append(
      element('span', 'device-icon', device.kind === 'server' ? '🖥️' : '📱'),
      element('span', 'device-name', device.name),
      element('span', 'device-state', device.kind === 'server' ? 'This PC' : 'Online')
    );
    button.addEventListener('click', () => onSelect(device.id));
    list.append(button);
  }
}

function progressRow(label, sublabel, ratio, statusText) {
  const item = element('li', 'transfer');
  const head = element('div', 'transfer-head');
  head.append(element('span', 'transfer-name', label), element('span', 'transfer-size', sublabel));

  const track = element('div', 'progress');
  const bar = element('div', 'progress-bar');
  bar.style.width = `${Math.round(ratio * 100)}%`;
  track.append(bar);

  item.append(head, track, element('div', 'transfer-status', statusText));
  return item;
}

const UPLOAD_LABELS = {
  queued: 'Waiting…',
  starting: 'Starting…',
  uploading: 'Sending',
  completed: 'Sent',
  cancelled: 'Cancelled',
  failed: 'Failed',
};

export function renderUploads(queue, onCancel) {
  const list = elements['upload-list'];
  list.replaceChildren();
  elements['uploads-panel'].hidden = queue.uploads.length === 0;

  for (const upload of queue.uploads) {
    const percent = Math.round(upload.progress * 100);
    const status =
      upload.status === 'failed'
        ? `Failed — ${upload.error}`
        : upload.status === 'uploading'
          ? `${UPLOAD_LABELS.uploading} — ${percent}% of ${formatBytes(upload.file.size)}`
          : UPLOAD_LABELS[upload.status] || upload.status;

    const item = progressRow(
      upload.file.name,
      formatBytes(upload.file.size),
      upload.progress,
      status
    );
    item.dataset.status = upload.status;

    if (['queued', 'starting', 'uploading'].includes(upload.status)) {
      const cancel = element('button', 'link-button', 'Cancel');
      cancel.type = 'button';
      cancel.addEventListener('click', () => onCancel(upload));
      item.append(cancel);
    }
    list.append(item);
  }
}

export function renderIncoming(items, onDismiss, onSave) {
  const list = elements['incoming-list'];
  list.replaceChildren();
  elements['incoming-panel'].hidden = items.length === 0;

  for (const item of items) {
    const row = element('li', 'transfer');
    row.dataset.status = item.ready ? 'completed' : 'uploading';

    const head = element('div', 'transfer-head');
    head.append(
      element('span', 'transfer-name', item.filename),
      element('span', 'transfer-size', formatBytes(item.size))
    );
    row.append(head);

    if (item.ready) {
      row.append(element('div', 'transfer-status', `From ${item.senderName}`));
      // A button, not a link: the download endpoint needs an Authorization
      // header now, and an <a href> cannot send one.
      const save = element('button', 'button-primary', 'Save file');
      save.type = 'button';
      save.addEventListener('click', () => onSave(item));
      row.append(save);

      const dismiss = element('button', 'link-button', 'Dismiss');
      dismiss.type = 'button';
      dismiss.addEventListener('click', () => onDismiss(item.transferId));
      row.append(dismiss);
    } else {
      const percent = item.size ? Math.round((item.received / item.size) * 100) : 0;
      const track = element('div', 'progress');
      const bar = element('div', 'progress-bar');
      bar.style.width = `${percent}%`;
      track.append(bar);
      row.append(track, element('div', 'transfer-status', `Receiving from ${item.senderName}…`));
    }
    list.append(row);
  }
}

export function renderHistory(transfers, selfId, onDownload) {
  const list = elements['history-list'];
  list.replaceChildren();
  elements['history-empty'].hidden = transfers.length > 0;
  elements['clear-history'].hidden = transfers.length === 0;

  for (const transfer of transfers) {
    const row = element('li', 'history-row');
    row.dataset.status = transfer.status;

    const direction = transfer.sender_id === selfId ? 'Sent' : 'Received';
    row.append(
      element('span', 'history-name', transfer.filename),
      element(
        'span',
        'history-meta',
        `${direction} · ${formatBytes(transfer.size)} · ${formatTime(
          transfer.completed_at || transfer.created_at
        )}`
      ),
      element('span', 'history-status', transfer.status)
    );

    if (transfer.status === 'completed' && transfer.receiver_id === selfId) {
      const save = element('button', 'link-button', 'Download');
      save.type = 'button';
      save.addEventListener('click', () => onDownload(transfer));
      row.append(save);
    }
    list.append(row);
  }
}

export function renderPeers(peers) {
  const list = elements['peer-list'];
  list.replaceChildren();
  // Hidden entirely when there is nobody out there: an empty panel would just
  // look like something is broken.
  elements['peers-panel'].hidden = peers.length === 0;

  for (const peer of peers) {
    const row = element('li', 'peer-row');
    const link = element('a', 'peer-link', peer.name);
    link.href = peer.url;
    // A peer is a different server, so open it in its own tab rather than
    // navigating away from a transfer in progress.
    link.target = '_blank';
    link.rel = 'noopener noreferrer';

    row.append(link, element('span', 'peer-meta', `${peer.url} · v${peer.version}`));
    list.append(row);
  }
}

/**
 * Show or hide the "waiting for approval" screen.
 *
 * While it is up the rest of the page is hidden rather than merely covered:
 * an unapproved device can do nothing, so showing it a device list and a file
 * picker that would only fail is worse than showing nothing.
 */
export function setAwaitingApproval(waiting, { denied = false, offline = false, name = '' } = {}) {
  elements['waiting-screen'].hidden = !waiting;
  elements['waiting-denied'].hidden = !denied;
  // A failing poll looks exactly like a patient one. Say which it is.
  elements['waiting-offline'].hidden = !offline;
  if (name) elements['waiting-name'].textContent = name;

  // The host's own browser lands here whenever it opens the LAN address, and
  // the dead end is not obvious: the way out is the same page over loopback.
  const hostUrl = elements['waiting-host-url'];
  if (hostUrl) {
    const url = `${window.location.protocol}//localhost:${window.location.port || '8080'}/`;
    hostUrl.href = url;
    hostUrl.textContent = url;
  }

  const main = document.querySelector('main');
  const footer = document.querySelector('footer');
  if (main) main.hidden = waiting;
  if (footer) footer.hidden = waiting;
}

export function renderRequests(requests, onDecide) {
  const list = elements['request-list'];
  list.replaceChildren();
  elements['requests-panel'].hidden = requests.length === 0;

  for (const request of requests) {
    const row = element('li', 'pending-row');
    const details = element('div', 'pending-details');
    details.append(
      element('span', 'pending-name', request.filename),
      element(
        'span',
        'pending-meta',
        `${formatBytes(request.size)} from ${request.senderName}`
      )
    );

    const actions = element('div', 'pending-actions');
    const accept = element('button', 'button-primary', 'Accept');
    accept.type = 'button';
    accept.addEventListener('click', () => onDecide(request.transferId, 'accept'));

    const reject = element('button', 'button-quiet', 'Reject');
    reject.type = 'button';
    reject.addEventListener('click', () => onDecide(request.transferId, 'decline'));

    actions.append(accept, reject);
    row.append(details, actions);
    list.append(row);
  }
}

export function renderPending(devices, onDecide) {
  const list = elements['pending-list'];
  list.replaceChildren();
  elements['pending-panel'].hidden = devices.length === 0;

  for (const device of devices) {
    const row = element('li', 'pending-row');
    const details = element('div', 'pending-details');
    details.append(
      element('span', 'pending-name', device.name),
      element('span', 'pending-meta', device.first_address || 'unknown address')
    );

    const actions = element('div', 'pending-actions');
    const allow = element('button', 'button-primary', 'Allow');
    allow.type = 'button';
    allow.addEventListener('click', () => onDecide(device.id, 'approve'));

    const deny = element('button', 'button-quiet', 'Deny');
    deny.type = 'button';
    deny.addEventListener('click', () => onDecide(device.id, 'deny'));

    actions.append(allow, deny);
    row.append(details, actions);
    list.append(row);
  }
}

let toastTimer = null;

export function toast(message, kind = 'info') {
  const node = elements.toast;
  node.textContent = message;
  node.dataset.kind = kind;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.hidden = true;
  }, 4000);
}
