# LANShare

**Send files between devices on the same Wi-Fi. Open a page, pick a device, send the file.**

No cloud, no accounts, no app to install. The server runs on your PC; every other device is
just a browser. Files never leave your local network — there is no outbound call anywhere in
the codebase.

```
 Browser (iPhone / Android / PC)
   │  REST       upload, download, device info
   │  WebSocket  progress, device join/leave, pairing
   ▼
 FastAPI server (your PC)
   ├── SQLite       devices, transfers, settings
   └── File system  storage/incoming, storage/temporary
```

---

## Contents

- [Why this exists](#why-this-exists)
- [Quick start](#quick-start)
- [Using it](#using-it)
- [How a transfer works](#how-a-transfer-works)
- [Who is allowed in](#who-is-allowed-in)
- [Finding the server](#finding-the-server)
- [Optional: HTTPS](#optional-https)
- [Configuration](#configuration)
- [HTTP API](#http-api)
- [WebSocket events](#websocket-events)
- [Project layout](#project-layout)
- [Development](#development)
- [Design notes](#design-notes)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)

---

## Why this exists

Moving a photo from a phone to a PC on the same desk usually means a cable, a cloud round
trip, or emailing yourself. LANShare is the boring local answer: both devices are already on
the same network, so the bytes should go directly across it.

Design constraints that shaped everything else:

- **Browsers cannot open raw sockets.** A phone cannot discover the server by itself, so
  pairing happens by QR code, by mDNS name, or by typing an address.
- **Discovery and transfer are separate subsystems.** Discovery failing must never stop a
  transfer working.
- **iOS Safari suspends JavaScript when you switch apps.** Resumable uploads are a core
  requirement, not a nice-to-have.
- **The server is the only party with a filesystem.** Every transfer goes through it.

## Quick start

**Requirements:** Python 3.11+. Developed and tested on Windows 11; the code is
platform-neutral apart from the Windows-specific filename rules.

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m lanshare
```

> On Windows, `python` on `PATH` may be a Microsoft Store stub that prints "Python was not
> found" and exits. Use `py -3` to create the virtualenv; inside the activated venv,
> `python` works normally.

On macOS or Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m lanshare
```

You should see:

```
  LANShare is running
  On this PC      http://localhost:8080
  On your phone   http://192.168.1.20:8080
  Or by name      http://lanshare.local:8080
  Saving files to /home/you/lanshare/storage/incoming

  Point your phone's camera at this code:

  [ a scannable QR of the address ]

  Same Wi-Fi, no cloud. Ctrl+C to stop.
```

The banner prints during application startup, *before* the port is bound. If an `ERROR` line
follows `Application startup complete`, the server did not actually start — most often
`[Errno 10048] only one usage of each socket address`, meaning something already holds the
port.

A bare uvicorn command starts the app too, but do not use one: its
`--timeout-keep-alive` defaults to 5 seconds, which is shorter than it takes a person to
pick a video out of a phone's gallery. The browser then reuses a connection the server has
already closed, and because a `POST` is never retried automatically, the transfer hangs on
*Starting...* with no error. If you want auto-reload, pass the timeout yourself and keep
`--port` in step with `LANSHARE_PORT`:

```powershell
uvicorn lanshare.main:app --host 0.0.0.0 --port 8080 --reload --app-dir server --timeout-keep-alive 120
```

### One click instead of a command

Run this once to create the app icon, `LANShare.lnk`, in the project folder:

```powershell
.\tools\install-shortcut.ps1
```

Double-clicking it starts the server in its own window, waits until the server answers, and
opens the page in your browser. Double-clicking it again when the server is already up just
opens the page instead of starting a second one.

To launch it from somewhere else - the desktop, a folder you keep open, the taskbar - copy
`LANShare.lnk` there, or right-click it and choose **Create shortcut**. A copied .lnk keeps
the icon and still points at the project, so the copies need nothing of their own. The
script can also place them for you: `-Desktop`, `-StartMenu`, and `-Remove` to delete the
ones it made.

The .lnk stores absolute paths, so it is not committed, and it stops working if you move
the project folder. Re-run the script to repair it.

### First run on Windows

Windows Firewall blocks inbound connections by default. The first time you start the server,
Windows asks whether to allow Python on private networks — **say yes**, or your phone cannot
reach the PC. If you dismissed the prompt, add an inbound rule for TCP 8080 on the private
profile.

## Using it

1. Start the server on your PC.
2. On your phone, scan the QR code in the terminal, or type the `On your phone` address into
   Safari or Chrome. On an iPhone or Mac, `http://lanshare.local:8080` works too.
3. **Approve the new device.** The phone shows *Waiting for approval*. On the PC open
   **`http://localhost:8080`** — not the LAN address — and press **Allow**.
4. Both devices now appear in each other's **Send to** list.
5. Pick a device, choose files, watch the progress bar.
6. The receiver gets a **Save file** button. Files also land in `storage/incoming/` on the PC.

> **Why `localhost` and not the LAN address?** Only the loopback address is trusted on sight.
> Once a request arrives over the LAN the server genuinely cannot tell your own browser from
> anyone else's, so the host's browser at `http://192.168.x.x:8080` is treated as a new
> device and queues for approval like any other. The waiting screen links to the loopback
> address for exactly this reason.

Each browser names itself from its user agent ("iPhone - Safari"). Tap the name in the top
right to change it.

### Where files land on the receiving device

On the PC, in `storage/incoming/` (configurable — see [Configuration](#configuration)).

On iOS, the browser decides: **Settings → Safari → Downloads** controls whether files go to
`iCloud Drive/Downloads` or `On My iPhone/Downloads`. If you want transfers to stay genuinely
local, set it to **On My iPhone** — the default sends everything you receive straight back up
to iCloud, which rather defeats the point.

## How a transfer works

The sender uploads to the server, the server notifies the receiver over a WebSocket, and the
receiver downloads. Files stream to disk in chunks; nothing is ever loaded whole into memory
on the server.

1. `POST /api/transfers` reserves a transfer and returns `chunk_size` and `total_chunks`.
2. The client `PUT`s each chunk to `/api/transfers/{id}/chunks/{index}` as a raw body. Each
   chunk lands in `storage/temporary/{transfer_id}/{index}.part`.
3. `POST /api/transfers/{id}/complete` concatenates the chunks in order, hashes the result
   with SHA-256, and moves it into `storage/incoming/`.

**Resume** is free: chunk state is derived by listing the temp directory, so the database can
never claim a chunk that is not on disk. A client that drops mid-upload calls
`GET /api/transfers/{id}`, gets back `missing_chunks`, and sends only those. Re-sending a
chunk is safe — it overwrites.

**Chunk size is chosen per transfer, not globally.** Listing the chunk directory is `O(chunks)`
and runs once per uploaded chunk, so scanning cost grows with the *square* of the chunk count.
Measured on a local SSD: 4,096 chunks costs ~150s of scanning spread across a transfer; 16,384
costs ~41 minutes. The server therefore doubles the chunk size until the file fits in 4,096
chunks:

| File size | Chunk | Chunks |
|---:|---:|---:|
| ≤ 16 GiB | 4 MiB | ≤ 4096 |
| 32 GiB | 8 MiB | 4096 |
| 64 GiB | 16 MiB | 4096 |
| 128 GiB | 32 MiB | 4096 |
| 256 GiB | 64 MiB | 4096 |

256 GiB is the hard ceiling on `LANSHARE_MAX_FILE_SIZE`, and it is not an arbitrary round
number: 64 MiB × 4,096 chunks *is* 256 GiB, the point at which the chunk size runs out and
the count starts climbing again. Raising it past that is a code change, not a config one.

**Disk:** budget roughly twice the file size free — the chunks and the assembled file coexist
until the move completes — or three times if `incoming` and `temporary` sit on different
drives, since the move becomes a copy.

**Integrity and safety.** SHA-256 is computed during assembly and stored. Filenames are
sanitised: directory components stripped, illegal and control characters replaced, Windows
reserved device names (`CON`, `NUL`, `COM1`…) escaped case-insensitively regardless of
extension, length capped at 180 characters. Name collisions never overwrite — a second
`photo.jpg` becomes `photo (1).jpg`, and the free name is claimed atomically with
`O_CREAT|O_EXCL` so two simultaneous transfers cannot both take it.

## Who is allowed in

Every device is issued a **token** when it registers, and starts as **pending** — it can do
nothing until an already-trusted device approves it.

The split matters: the token answers *who are you*, the trust state answers *what may you do*.
Issuing the token up front means there is no credential to hand over after approval.

| State | Meaning |
|---|---|
| `pending` | Registered, waiting. May only poll `GET /api/devices/me`. |
| `trusted` | Approved. May send, receive and approve others. |
| `blocked` | Refused and remembered. Disconnected immediately. |

- The machine running the server is trusted automatically over loopback, and the host row
  cannot be blocked — you can never lock yourself out.
- **Only the host approves.** A trusted phone can send and receive, but it cannot approve
  the next device: one approval would otherwise quietly beget another with nobody at the
  PC ever seeing it. Set `LANSHARE_APPROVAL_FROM_HOST_ONLY=false` if you want the old
  behaviour.
- **Deny** forgets a device entirely (it may ask again). **Block** remembers and refuses it.
- Files are readable only by the **two devices in that transfer** — being approved is not the
  same as being involved.
- Files are checked against the sender's own hash **when the sender could work one out**.
  A browser can only hash in a secure context, so this covers the host's browser at
  `http://localhost` (and everything, once HTTPS is on) for files up to 8 MiB; a phone on
  plain http sends no hash and nothing is compared. A mismatch fails the transfer and the
  file is not kept.
- Tokens are hashed with SHA-256 and compared with `secrets.compare_digest`. Not bcrypt: these
  are 256 bits from `secrets.token_urlsafe`, so there is no dictionary to defend against and a
  slow hash would only add latency.
- The WebSocket token travels in `Sec-WebSocket-Protocol`, never the query string — uvicorn
  writes the full request line to its access log.

Rate limits exist because registration *cannot* require a credential (a device needs an
identity before anyone can approve it): at most `LANSHARE_MAX_PENDING_DEVICES` may wait at
once, and loopback is exempt so a flood can never stop you approving your own phone.

> **What this does and does not do.** Over plain http the token is visible to anyone who can
> read traffic on your network. Pairing raises the bar from "anyone on the Wi-Fi can send you
> files" to "someone has to be approved once". It is **not** protection against an attacker
> already capturing packets on your LAN. For that, turn on HTTPS.

### Which hostnames the server answers to

The server refuses, with `400`, any request whose `Host` header is an ordinary registrable
domain. Addresses, single-label machine names and the reserved local suffixes (`.local`,
`.lan`, `.home.arpa`, `.internal`) are accepted, so every address this README tells you to
use keeps working.

This blocks **DNS rebinding**. A page on the internet cannot read a response from
`http://127.0.0.1:8080` - the same-origin policy forbids it - but it can point a domain it
owns at `127.0.0.1` after the page has loaded and then call the server as
`http://its-own-domain:8080`, which the browser treats as same-origin. The request arrives
from loopback, and loopback is trusted on sight here, so without this check such a page
would be issued a token for a trusted device: your device list, your transfer history and
every file you have received.

If you reach LANShare through a real domain name - a reverse proxy, a Tailscale address -
list it in `LANSHARE_ALLOWED_HOSTS` (comma-separated).

## Finding the server

Three independent mechanisms, none of which a transfer depends on:

- **QR code** — of the LAN URL, shown in the startup banner and on the PC's own page under
  **Add a device**. The QR encodes the IP, not the mDNS name, because the IP always resolves.
- **mDNS** — the server advertises `_http._tcp` with an A record, so `http://lanshare.local:8080`
  works on Apple devices out of the box. Most Android browsers cannot resolve `.local`.
- **UDP broadcast** — finds *other LANShare servers* on the network, listed under **Other
  LANShare servers**. Discovery only: they stay separate servers with their own devices and
  files, and nothing is ever sent between them.

The UDP layer is server-to-server because browsers cannot open raw sockets. Announcements are
unauthenticated by nature, so the parser caps the datagram at 1 KiB before decoding, validates
every field, takes the peer's address **from the packet source rather than its payload**, and
caps the peer table so one noisy neighbour cannot grow it without limit.

mDNS or UDP failing is logged and otherwise ignored — both are blocked often enough (VPNs,
guest networks, firewalls) that a failure must not take the app down with it.

## Optional: HTTPS

Off by default, because a self-signed certificate means a browser warning on every device and
a trust profile to install on iOS. Turn it on when you want a secure context — that is what
unlocks `crypto.subtle` and the Clipboard API.

Generate a certificate (openssl ships with Git for Windows):

```powershell
openssl req -x509 -newkey rsa:2048 -nodes -days 365 `
  -keyout key.pem -out cert.pem `
  -subj "/CN=lanshare.local" `
  -addext "subjectAltName=DNS:lanshare.local,DNS:localhost,IP:192.168.1.20,IP:127.0.0.1"
```

Then in `.env` (use your own IP above):

```ini
LANSHARE_ENABLE_HTTPS=true
LANSHARE_SSL_CERTFILE=cert.pem
LANSHARE_SSL_KEYFILE=key.pem
```

Certificates are supplied, not generated — generating them would mean another dependency. The
server **refuses to start** if the flag is on but the files are missing or unreadable; it will
not quietly serve plaintext when you asked for TLS.

On iOS the CA must be installed *and* enabled under Settings → General → About → Certificate
Trust Settings.

## Configuration

Every setting is an environment variable prefixed `LANSHARE_`, or a line in `.env` at the repo
root. Copy [`.env.example`](.env.example) to get started. Relative paths resolve against the
repo root.

| Variable | Default | What it does |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address. `0.0.0.0` is what makes the server reachable from other devices. |
| `PORT` | `8080` | HTTP port. |
| `SERVER_NAME` | `LANShare PC` | How the host PC appears in device lists. |
| `INCOMING_DIR` | `storage/incoming` | Where completed files land. Point it at a real folder if you like. |
| `TEMPORARY_DIR` | `storage/temporary` | Partial chunks during upload. |
| `DATA_DIR` | `data` | SQLite database location. |
| `CHUNK_SIZE` | `4194304` (4 MiB) | The **smallest** chunk used; large files get a bigger one automatically. |
| `MAX_FILE_SIZE` | `274877906944` (256 GiB) | Per-file ceiling. Also the maximum accepted value. |
| `MAX_PENDING_DEVICES` | `20` | How many devices may await approval at once. |
| `DEVICE_RETENTION_DAYS` | `30` | How long device rows survive so history can still name them. |
| `ENABLE_MDNS` | `true` | Advertise `lanshare.local`. |
| `MDNS_HOSTNAME` | `lanshare` | The `.local` name to claim. |
| `ENABLE_UDP_DISCOVERY` | `true` | Find other LANShare servers. |
| `DISCOVERY_PORT` | `8079` | UDP broadcast port. |
| `ANNOUNCE_INTERVAL_SECONDS` | `10.0` | How often to announce. |
| `PEER_TTL_SECONDS` | `35.0` | How long a silent peer is remembered. |
| `ENABLE_HTTPS` | `false` | See [Optional: HTTPS](#optional-https). |
| `SSL_CERTFILE` / `SSL_KEYFILE` | unset | Required together when HTTPS is on. |

## HTTP API

Interactive docs at `/docs` while the server runs.

Authenticated endpoints need **both** `X-Device-Id` and `Authorization: Bearer <token>`.

- `✓` — authenticated and `trusted`
- `auth` — authenticated, any trust state
- `participant` — additionally restricted to the transfer's sender and receiver

| Method | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/api/health` | open | Liveness, server name, version |
| `POST` | `/api/devices/register` | open | New device → `{device, token}`; refreshing needs the token |
| `GET` | `/api/server-info` | ✓ | LAN URL, port, host device id, chunk size, limits, QR SVG |
| `GET` | `/api/devices/me` | auth | This device's own record, any trust state — what a waiting device polls |
| `GET` | `/api/devices/pending` | ✓ | Devices awaiting approval |
| `POST` | `/api/devices/{id}/trust` | ✓ | `{"decision": "approve"\|"deny"\|"block"}` |
| `GET` | `/api/devices` | ✓ | Trusted devices that can receive right now |
| `POST` | `/api/transfers` | ✓ | Init → `transfer_id`, `chunk_size`, `total_chunks` |
| `PUT` | `/api/transfers/{id}/chunks/{index}` | ✓ sender | Upload one chunk, raw body |
| `GET` | `/api/transfers/{id}` | ✓ participant | Status + `received_chunks` / `missing_chunks` |
| `POST` | `/api/transfers/{id}/complete` | ✓ sender | Assemble, hash, move to incoming |
| `DELETE` | `/api/transfers/{id}` | ✓ participant | Cancel and delete partial chunks |
| `GET` | `/api/transfers?limit=` | ✓ | History, always scoped to the caller |
| `GET` | `/api/files/{id}/download` | ✓ participant | Stream the assembled file |
| `GET` | `/api/peers` | ✓ | Other LANShare servers found over UDP |

### Status codes

| Code | When |
|---|---|
| `400` | Chunk index out of range, or a chunk that is not the expected length |
| `401` | No credential, malformed `Authorization`, or a token that does not match. **Missing and wrong are deliberately identical**, so this cannot be used to discover which device ids exist |
| `403` | Device is `pending` or `blocked`; not the sender; not a participant; receiver not approved |
| `404` | Unknown transfer or receiver; file gone from disk |
| `409` | Completing with chunks missing; acting on a finished transfer |
| `413` | Declared size above `MAX_FILE_SIZE` |
| `422` | Malformed body or path parameter — *not* a missing credential |
| `429` | Too many devices already awaiting approval |

Errors are `{"detail": "..."}`. Stack traces and filesystem paths never reach a client.

## WebSocket events

`/ws?device_id=...`, with the token in the `Sec-WebSocket-Protocol` header as
`["lanshare.v1", "token.<value>"]`. All frames are `{"type": "...", "data": {...}}`.

**Server → client:** `device.list`, `device.joined`, `device.left`, `device.pending`,
`device.pending.list`, `device.approved`, `device.denied`, `transfer.incoming`,
`transfer.progress`, `transfer.completed`, `transfer.failed`, `transfer.cancelled`, `pong`

**Client → server:** `ping`, `device.rename`

Close codes:

| Code | Meaning | Client behaviour |
|---|---|---|
| `4404` | Unknown device, or the token does not match | Re-register, then reconnect |
| `4403` | Known but not approved, or trust revoked | Show the waiting screen and poll; do not retry |

The socket is **accepted before it is refused**, so these codes survive. Closing before
`accept()` makes an ASGI server fail the handshake with HTTP 403, and the browser only ever
sees code 1006 — indistinguishable from the server being down.

The client reconnects with exponential backoff, and reconnects immediately on
`visibilitychange` rather than waiting out the backoff, because iOS kills the socket as soon
as the tab is backgrounded.

## Project layout

```
server/lanshare/
├── main.py              # App factory, lifespan, WebSocket endpoint, static mount
├── config.py            # Settings (pydantic-settings)
├── models.py            # Pydantic request/response schemas — the REST contract
├── api/                 # Thin routers: health, devices, transfers, files, peers
│   └── deps.py          # Authentication: identify, then authorise
├── services/
│   ├── transfer_service.py  # Transfer lifecycle
│   ├── storage.py           # Paths, filename safety, chunk writes, assembly
│   ├── auth.py              # Tokens, hashing, loopback detection
│   └── network.py           # LAN IP detection
├── discovery/           # qr.py, mdns.py, udp.py
├── ws/manager.py        # Connection registry and broadcast
└── db/                  # database.py, schema.sql, migrations/, repositories.py

frontend/                # No build step, no npm, no framework
├── index.html
├── css/styles.css
└── js/                  # app, api, ws, upload, devices, ui (ES modules)

tools/                   # Desktop launcher: LANShare.cmd, the .ps1 behind it,
                         # install-shortcut.ps1, and make_icon.py (draws the .ico)

tests/                   # 321 tests
```

Routers stay thin, logic lives in `services/`, and **all** SQL lives in
`db/repositories.py` — parameterised, always.

## Development

```powershell
pytest                      # 247 tests
ruff check .
ruff format .
mypy server
```

`schema.sql` is a frozen baseline applied on every startup; every change after it is a
numbered migration in `db/migrations/`, tracked in a `schema_migrations` table and applied
once. A fresh database gets the baseline plus every migration; an existing one gets only what
it is missing.

### Notes on the test suite

Some tests exist because a specific bug shipped, and their docstrings say so. Three are worth
knowing about because they guard against things a normal test cannot see:

- `test_frontend_assets.py` parses the shipped HTML, CSS and JS. The `hidden` attribute loses
  a specificity tie to any class that sets `display`, which once left a full-screen overlay
  permanently visible on top of a working page — with the attribute correctly set in the DOM.
  Another test asserts every API call in `api.js` sends its credential.
- `test_hardening.py::test_a_refused_socket_is_accepted_first` asserts the ASGI **message
  order**, not the close code. Starlette's `TestClient` speaks ASGI with no handshake, so it
  reports application close codes that a real browser never receives.
- `test_chunk_sizing.py::test_the_ceiling_is_exactly_where_the_chunk_size_runs_out` asserts
  two constants stay consistent, because raising one alone produces a slow transfer rather
  than an error.

## Design notes

Decisions that are not obvious from reading the code:

**"Online" means "holds a WebSocket connection."** Not "registered recently". A device you
cannot push an event to cannot receive a file, so the device list is built from the connection
registry rather than a timestamp.

**Transfer rows carry `stored_name` separately from `filename`.** The name the sender asked
for and the name actually written differ whenever sanitising or collision-renaming kicks in,
and history needs both.

**A peer's address comes from the packet, never its payload.** A UDP announcement is
unauthenticated input from the network; letting it declare its own address would let anyone
redirect the peer list.

**mDNS failure is never fatal.** Advertising is best-effort and blocked often enough that
treating it as required would make the app fail for reasons unrelated to transferring files.

**Identity and authority are separate.** A token is issued at registration, before approval.
This is why there is no second credential exchange after someone presses Allow.

**HTTPS is opt-in.** A self-signed certificate degrades first-run experience on every device;
making it the default would trade a real cost for a benefit most LAN users do not need.

**Chunk size scales with the file.** Covered above — the resume scan is quadratic in chunk
count, so a fixed chunk size made "just raise the limit" a trap that looks like a config edit
and behaves like a rewrite.

## Troubleshooting

**My phone says "Waiting for approval" and nothing happens.** Approve it on the PC at
`http://localhost:8080` — not the LAN address. Only loopback is trusted on sight.

**The PC itself says "Waiting for approval".** You opened the LAN address on the machine
running the server. Follow the `localhost` link on that screen — same page, trusted origin.

**`[Errno 10048] only one usage of each socket address`.** Something already holds port 8080,
often an earlier LANShare you forgot to stop. On Windows:
`Get-NetTCPConnection -State Listen -LocalPort 8080 | Select-Object OwningProcess`

**My phone used to work and now asks for approval again.** Expected after upgrading to the
pairing release: existing devices were reset to pending because they predate tokens and had no
credential to authenticate with.

**The phone cannot open the address.** Probably a guest network. Many routers enable "client
isolation" on guest Wi-Fi, which blocks devices from talking to each other. Use the main
network.

**The printed address is wrong.** A VPN, Hyper-V or WSL adapter can win the default route and
get picked instead of your Wi-Fi adapter. Check `ipconfig` and open the correct address by
hand.

**Uploads stall when I switch apps on iPhone.** Expected — iOS suspends the tab. Return to
Safari and the upload resumes from the last finished chunk.

**Another LANShare server is not showing up.** UDP broadcast on port 8079; Windows Firewall
may block it and most guest networks drop broadcast entirely. Allow ~15s to appear, and ~35s
to disappear for a server that did not shut down cleanly. None of this affects transfers.

**`lanshare.local` does not work.** mDNS can be blocked by a VPN, a firewall or a guest
network, and most Android browsers cannot resolve `.local` at all. The IP address always
works — which is why the QR encodes the IP. If the banner has no `Or by name` line,
advertising failed and the log says why.

**The QR in the terminal will not scan.** Some terminals ignore the background colours it is
drawn with. Use the QR on the PC's web page instead, under **Add a device**.

## Limitations

Known and deliberate, as of now:

- **Receiving in a browser buffers the whole file.** The download endpoint needs an
  `Authorization` header and a plain `<a download>` cannot send one, so the page fetches the
  bytes and saves them from a blob URL. Sending is streamed and effectively unbounded;
  receiving on a phone is not. The practical ceiling on iOS is well below the 256 GiB send
  limit and has not been measured.
- **Plain http by default.** See the security note above.
- **One server, one PC.** Peers are discovered but never federated.
- **Folders, text snippets and clipboard sync** are not implemented.
- **No CI yet.** `pytest`, `ruff` and `mypy` are run by hand.

## License

No license file yet, which means default copyright applies — all rights reserved. Add a
`LICENSE` file (MIT and Apache-2.0 are the usual choices for something like this) if you want
others to be able to use it.
