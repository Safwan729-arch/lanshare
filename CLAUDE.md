# CLAUDE.md — LANShare

> This file is loaded at the start of every Claude Code session.
> It holds the rules, the stack, and the map. Detailed knowledge lives in the Obsidian vault at `vault/`.
> **Read Section 1 first. Always.**

---

## 1. Session Protocol (Obsidian Second Brain) — MANDATORY

The Obsidian vault at `vault/` is your long-term memory. Context gets cleared between tasks. The vault does not.
Your goal: recover the context you need **cheaply**, by reading a few small notes instead of scanning the codebase.

### 1.1 On session start (or after `/clear`)
1. Read `vault/00-Index.md` (map of the vault).
2. Read `vault/01-Status/current-state.md` (what is done, in progress, blocked, next).
3. Read `vault/01-Status/handoff.md` (notes left by the previous session).
4. Read **only** the notes relevant to the current task (use the Index and `[[wikilinks]]`).
5. To find code, read `vault/02-Architecture/code-map.md` first. Do **not** run broad `ls -R`, `grep -r`, or open many source files to "get oriented".

### 1.2 During a task
- If you make a design decision, write an ADR in `vault/03-Decisions/` (see template).
- If you hit a non-obvious bug or platform quirk, add it to `vault/06-Knowledge/gotchas.md`.
- If you add/move/rename a module, update `vault/02-Architecture/code-map.md`.
- If you add/change an endpoint or WebSocket event, update `api-endpoints.md` or `websocket-events.md`.
- If you change the DB schema, update `database-schema.md`.

### 1.3 On task end (before the user clears context)
1. Update `vault/01-Status/current-state.md` (tick finished items, add new ones).
2. Overwrite `vault/01-Status/handoff.md` with: what you did, what is half-done, exact next step, open questions.
3. Append a short log to `vault/05-Sessions/YYYY-MM-DD.md` (create if missing).
4. Keep notes short. Bullet points. Link, don't duplicate.

### 1.4 Vault writing rules
- Plain Markdown + YAML frontmatter. Use `[[wikilinks]]` between notes.
- One topic per note. If a note grows past ~150 lines, split it.
- The vault describes **what and why**. The code is the source of truth for **how**. Never paste large code blocks into the vault — link to the file path instead.
- Never delete a note without asking the user. Mark outdated content with `> [!warning] Outdated` instead.
- Do not edit `vault/.obsidian/` (the user's Obsidian settings).

### 1.5 Vault structure
```
vault/
├── 00-Index.md                  # Map of content. Entry point.
├── 01-Status/
│   ├── current-state.md         # Roadmap checklist + current focus
│   └── handoff.md               # Last session → next session
├── 02-Architecture/
│   ├── overview.md              # System design, diagrams
│   ├── code-map.md              # Where each responsibility lives in the repo
│   ├── api-endpoints.md         # REST contract
│   ├── websocket-events.md      # WS message contract
│   ├── transfer-protocol.md     # Chunked upload / resume / integrity
│   ├── discovery.md             # QR, mDNS, UDP broadcast
│   └── database-schema.md       # SQLite tables
├── 03-Decisions/                # ADR-0001-title.md, ADR-0002-...
├── 04-Tasks/                    # One note per larger feature/task
├── 05-Sessions/                 # YYYY-MM-DD.md logs
├── 06-Knowledge/
│   ├── gotchas.md               # Platform quirks & hard-won lessons
│   └── glossary.md              # Terms (mDNS, chunk, ADR, etc.)
└── 07-Templates/                # adr.md, task.md, session.md
```
If the vault or any of these notes is missing, create it using the templates in Section 13.

---

## 2. Project Summary

**LANShare** is a self-hosted, open-source web app for sending files between devices on the **same local network** (Wi-Fi/LAN).

User flow: *connect to the same Wi-Fi → open a webpage → pick a device → send the file.*

- No cloud. No accounts. No native mobile app. Any modern browser is a client.
- The server runs on the user's Windows PC (v1).
- **Core principle:** files travel over the local network only. Never through the internet.
- **First target:** iPhone Safari ⇄ Windows PC. Get this rock-solid before expanding.

---

## 3. Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | Type hints everywhere |
| Web framework | FastAPI | REST + WebSocket + static files |
| ASGI server | Uvicorn | Bind to `0.0.0.0`, default port `8080` |
| Uploads | `python-multipart`, `aiofiles` | Stream to disk; never load a whole file into RAM |
| Database | SQLite via `aiosqlite` | Plain SQL in `schema.sql`; no ORM in v1 |
| Validation | Pydantic v2 | Request/response models |
| Settings | `pydantic-settings` | Env vars + `.env` |
| QR codes | `segno` | Generates QR for the LAN URL |
| mDNS | `zeroconf` | Advertise `lanshare.local` / `_http._tcp` |
| Frontend | HTML + CSS + vanilla JS (ES modules) | **No build step, no framework, no npm** in v1 |
| Real-time | Native browser `WebSocket` | Progress, device join/leave, transfer events |
| Tests | `pytest`, `pytest-asyncio`, `httpx` | |
| Lint/format | `ruff` (lint + format) | |
| Types | `mypy` (optional, non-blocking) | |

**Do not add** new dependencies without asking the user first and recording an ADR.
**Do not introduce** React, a bundler, or a CSS framework unless an ADR approves it.

---

## 4. Commands (Windows, PowerShell)

```powershell
# Setup
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Run (dev)
uvicorn lanshare.main:app --host 0.0.0.0 --port 8080 --reload --app-dir server

# Test / lint / format
pytest
ruff check .
ruff format .
```
The server must print the LAN URL (e.g. `http://192.168.1.20:8080`) and show a QR code on startup.

---

## 5. Repository Layout

```
lanshare/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── .env.example
├── server/
│   └── lanshare/
│       ├── main.py              # App factory, startup/shutdown, static mount
│       ├── config.py            # Settings (port, storage dirs, limits)
│       ├── models.py            # Pydantic schemas
│       ├── api/
│       │   ├── health.py
│       │   ├── devices.py
│       │   ├── transfers.py
│       │   └── files.py
│       ├── ws/
│       │   └── manager.py       # Connection registry + broadcast helpers
│       ├── services/
│       │   ├── transfer_service.py
│       │   ├── storage.py       # Paths, filename sanitizing, disk writes
│       │   └── network.py       # Detect LAN IP
│       ├── discovery/
│       │   ├── qr.py
│       │   ├── mdns.py
│       │   └── udp.py           # Phase 4 only
│       └── db/
│           ├── database.py
│           ├── schema.sql
│           └── repositories.py
├── frontend/
│   ├── index.html
│   ├── css/styles.css
│   └── js/
│       ├── app.js               # Entry point
│       ├── api.js               # fetch wrappers
│       ├── ws.js                # WebSocket client + reconnect
│       ├── upload.js            # Chunked upload + resume
│       ├── devices.js           # Device list + identity
│       └── ui.js                # DOM rendering, progress bars
├── storage/
│   ├── incoming/                # Completed files (default)
│   └── temporary/               # Partial chunks
├── data/lanshare.db             # gitignored
├── tests/
└── vault/                       # Obsidian second brain (see Section 1)
```
Keep `vault/02-Architecture/code-map.md` in sync with this tree.

---

## 6. Architecture

```
 Browser (iPhone / Android / PC)
   │  REST (upload, download, device info)
   │  WebSocket (progress, events)
   ▼
 FastAPI server (Windows PC)
   │
   ├── SQLite (devices, transfers, settings)
   └── File system (storage/incoming, storage/temporary)
```

### Key facts that shape the design
1. **Browsers cannot do UDP or raw sockets.** A phone's browser cannot discover the server by itself. Phone discovery = **QR code** or **mDNS name** or manual URL. UDP broadcast is only for server-to-server discovery (Phase 4).
2. **Discovery and transfer are separate subsystems.** Keep them in separate modules. Never couple them.
3. **All transfers go through the server (v1).** "Send to device X" = sender uploads to server → server notifies X over WebSocket → X downloads. If X is the host PC itself, the file is simply saved to the download folder.
4. **Browser devices have no hostname.** Device name = user-editable name, defaulted from the user agent (e.g. "iPhone – Safari"). Device ID = random UUID stored in `localStorage` + sent with every request.

---

## 7. Contracts (summary — full detail in the vault)

### 7.1 REST (`vault/02-Architecture/api-endpoints.md`)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness + server name + version |
| GET | `/api/server-info` | LAN URL, QR (SVG), server device name |
| POST | `/api/devices/register` | Register/refresh a browser device |
| GET | `/api/devices` | Online devices |
| POST | `/api/transfers` | Init transfer → `transfer_id`, `chunk_size` |
| PUT | `/api/transfers/{id}/chunks/{index}` | Upload one chunk (raw body) |
| GET | `/api/transfers/{id}` | Status + list of received chunk indexes (for resume) |
| POST | `/api/transfers/{id}/complete` | Assemble, verify, move to incoming |
| DELETE | `/api/transfers/{id}` | Cancel + clean temp files |
| GET | `/api/transfers` | History |
| GET | `/api/files/{id}/download` | Stream the file to the receiver |

### 7.2 WebSocket `/ws?device_id=...` (`websocket-events.md`)
All messages are JSON: `{ "type": "...", "data": {...} }`
- Server → client: `device.joined`, `device.left`, `device.list`, `transfer.incoming`, `transfer.progress`, `transfer.completed`, `transfer.failed`, `transfer.cancelled`
- Client → server: `ping`, `device.rename`
- Client must auto-reconnect with backoff. Server must tolerate abrupt disconnects.

### 7.3 Transfer protocol (`transfer-protocol.md`)
- Default chunk size: **4 MiB** (configurable).
- Chunks written to `storage/temporary/{transfer_id}/{index}.part`.
- Resume: client calls `GET /api/transfers/{id}`, re-sends only missing chunks.
- Each chunk upload is idempotent (re-sending the same index overwrites safely).
- On complete: server assembles in order, computes SHA-256, stores hash in DB, moves file to incoming dir.
- Name collisions: `photo.jpg` → `photo (1).jpg`. Never overwrite.

### 7.4 Database (`database-schema.md`)
Tables: `devices`, `transfers`, `settings`.
`transfers`: `id, filename, size, sha256, sender_id, receiver_id, status, chunk_size, total_chunks, created_at, completed_at`.
Status values: `pending | uploading | completed | failed | cancelled`.
Schema changes = numbered SQL migration + ADR.

---

## 8. Roadmap (mirror in `vault/01-Status/current-state.md`)

**Phase 1 — Core transfer (no discovery)**
- [ ] FastAPI app, config, static frontend served at `/`
- [ ] Print LAN URL on startup
- [ ] Device registration + device list
- [ ] Chunked upload with resume + cancel
- [ ] Download for receiving device
- [ ] WebSocket progress + device join/leave
- [ ] SQLite transfer history
- [ ] Multiple files per send
- [ ] Tested end-to-end: iPhone Safari ⇄ Windows PC

**Phase 2 — QR pairing**
- [ ] QR of LAN URL in terminal + on the PC's web page

**Phase 3 — mDNS**
- [ ] Advertise via `zeroconf`, reachable at `lanshare.local`

**Phase 4 — Server-to-server discovery**
- [ ] UDP broadcast between LANShare instances

**Phase 5 — Security**
- [ ] Device pairing with user approval on the host
- [ ] Token-based auth for trusted devices
- [ ] Encrypted transport (HTTPS with a local cert) — use established libraries only

**Later:** folders, text/link sharing, clipboard sync, screenshots, CLI (`lanshare send`, `lanshare devices`), React migration if the UI outgrows vanilla JS.

Do not start a later phase until the user confirms the current one is done.

---

## 9. Skills & Domain Knowledge Required

Claude Code must apply these competencies. Deeper notes belong in `vault/06-Knowledge/`.

**Backend (Python / FastAPI)**
- Async Python: `async/await`, avoiding blocking I/O in the event loop (use `aiofiles`, `run_in_threadpool` for CPU work like hashing large files).
- FastAPI routers, dependencies, lifespan events, `StreamingResponse` / `FileResponse`, `UploadFile` vs raw `Request.stream()`.
- WebSocket connection management: registry, broadcast, cleanup on disconnect.
- Pydantic v2 models and settings.
- SQLite with `aiosqlite`: parameterized queries only, WAL mode, simple migrations.

**Networking**
- LAN basics: private IP ranges, binding `0.0.0.0` vs `127.0.0.1`, detecting the correct LAN interface IP on Windows (ignore VPN/virtual adapters).
- mDNS / DNS-SD with `zeroconf`. UDP broadcast (Phase 4).
- Windows Firewall: inbound rule for the port; explain it to the user, don't silently change it.
- Wi-Fi client isolation (guest networks block device-to-device traffic — not a bug).

**Frontend (vanilla JS)**
- ES modules, `fetch`, `Blob.slice()` for chunking, `XMLHttpRequest` upload progress events (fetch has no upload progress), drag-and-drop API, `<input type="file" multiple>`.
- Mobile-first responsive CSS; large touch targets; works on iPhone Safari.
- WebSocket reconnect with exponential backoff.

**File handling**
- Streaming large files to/from disk without loading them into memory.
- Safe filenames: strip path separators, `..`, reserved Windows names (`CON`, `NUL`, …), control chars; cap length.
- Integrity via SHA-256.

**Testing**
- `pytest` + `httpx.AsyncClient` for API tests; test chunk resume, cancel, collisions, bad filenames.
- Manual device-test checklist in `vault/04-Tasks/`.

**Security mindset**
- Never trust client input (filenames, sizes, indexes, IDs).
- Never implement crypto by hand.

---

## 10. Platform Gotchas (seed for `vault/06-Knowledge/gotchas.md`)

- **`crypto.subtle` is unavailable on plain `http://192.168.x.x`** (secure context only). Do not rely on it in the browser. Hash on the server in v1.
- **Clipboard API also needs a secure context.** Relevant for the future clipboard feature → requires HTTPS (Phase 5).
- **iOS Safari suspends JS in the background.** Uploads stall if the user switches apps. This is why resume is in Phase 1, not "later".
- **iOS Safari file downloads** go to the Files app; use `Content-Disposition: attachment` with an RFC 5987 encoded filename for non-ASCII names.
- **HEIC photos** from iPhone may arrive as `.heic` or get converted to JPEG by Safari depending on settings. Don't assume the extension.
- **Windows Firewall** blocks inbound connections by default.
- **Multiple network adapters** (VPN, Hyper-V, WSL) can make IP detection pick the wrong address.

---

## 11. Coding Conventions

**Python**
- Type hints on all functions. `ruff` clean before finishing a task.
- Routers stay thin; logic goes in `services/`; SQL goes in `db/repositories.py`.
- Use `pathlib.Path`, never string paths. Resolve and check every path stays inside the storage root.
- Log with the `logging` module, not `print` (startup banner is the only exception).
- Errors: raise `HTTPException` with clear messages; never leak stack traces to clients.

**JavaScript**
- ES modules, `const`/`let`, no globals, no jQuery.
- One responsibility per module (see Section 5).
- All server calls go through `api.js`.

**General**
- Small, focused commits. Conventional commit style: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`.
- Comments explain *why*, not *what*.
- Prefer simple over clever. This is also a learning project — keep code readable.

---

## 12. Definition of Done (every task)

1. Code works and is `ruff` clean.
2. Tests added/updated and passing (`pytest`).
3. Relevant vault notes updated (Section 1.2).
4. `current-state.md` and `handoff.md` updated (Section 1.3).
5. You told the user how to test it manually (esp. on the iPhone).

---

## 13. Vault Templates

**`07-Templates/adr.md`**
```markdown
---
type: decision
id: ADR-XXXX
status: proposed | accepted | superseded
date: YYYY-MM-DD
tags: [decision]
---
# ADR-XXXX: Title
## Context
## Decision
## Consequences
## Related
- [[...]]
```

**`07-Templates/task.md`**
```markdown
---
type: task
status: todo | doing | done | blocked
phase: 1
tags: [task]
---
# Task: Title
## Goal
## Files touched
## Steps
- [ ]
## Notes / blockers
## Related
- [[...]]
```

**`07-Templates/session.md`**
```markdown
---
type: session
date: YYYY-MM-DD
tags: [session]
---
# Session YYYY-MM-DD
## Did
## Learned
## Next
```

**`01-Status/handoff.md`** (overwrite each session)
```markdown
---
type: handoff
updated: YYYY-MM-DD
---
# Handoff
## Last task
## State: done / partial
## Exact next step
## Open questions for the user
```

---

## 14. Things You Must Never Do

- Send any file or metadata to the internet. LANShare is LAN-only.
- Write outside `storage/`, `data/`, `vault/`, and the configured download directory at runtime.
- Add dependencies, frameworks, or build tools without asking.
- Roll your own cryptography.
- Scan the whole codebase to get oriented — use the vault.
- Skip the end-of-task vault update.
