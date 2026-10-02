"""Regressions from the security and reliability audit.

Every test here was written against a defect that was reproduced first, on this
code, before anything was changed. Each one says what the failure looked like,
because the fixes are small and easy to undo by accident.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from conftest import LOOPBACK_CLIENT, headers, register, send_file
from httpx import ASGITransport, AsyncClient
from lanshare.config import Settings
from lanshare.db.repositories import TransferRepository
from lanshare.main import create_app
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest_asyncio.fixture
async def client_for_host(app):
    """A loopback client that calls the server under a hostname of our choosing."""

    def make(hostname: str) -> AsyncClient:
        transport = ASGITransport(app=app, client=LOOPBACK_CLIENT)
        return AsyncClient(transport=transport, base_url=f"http://{hostname}")

    return make


# -- DNS rebinding: the Host header decides whether we answer at all ---------


@pytest.mark.asyncio
async def test_a_request_for_an_outside_hostname_is_refused(client_for_host) -> None:
    """The precondition for DNS rebinding, which this server used to meet.

    A page on the internet cannot read a response from http://127.0.0.1:8080 -
    the same-origin policy stops it. It *can* own a domain, point it at
    127.0.0.1 after the page has loaded, and then call the server as
    `http://evil.example.com:8080`, which the browser treats as same-origin
    with the attacker's page. The requests arrive from loopback, and loopback
    registers as trusted, so the page would be handed a trusted device token.

    The Host header is the one part the attacker cannot forge: it is their own
    domain, because that is what the browser connected to.
    """
    async with client_for_host("evil.example.com") as http:
        assert (await http.get("/api/health")).status_code == 400

        registered = await http.post(
            "/api/devices/register",
            json={"device_id": str(uuid.uuid4()), "name": "Attacker", "user_agent": "x"},
        )
        assert registered.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostname",
    [
        "127.0.0.1:8080",
        "localhost:8080",
        "192.168.1.20:8080",
        "[::1]:8080",
        "lanshare.local:8080",
        "DESKTOP-7F2K1",  # a Windows machine name, as typed on the LAN
        "printer.lan",  # a reserved local suffix, not a registrable domain
    ],
)
async def test_the_addresses_people_actually_use_still_work(client_for_host, hostname) -> None:
    """The fix must not cost anyone their way in.

    Every address the README tells a user to type, plus the shapes a LAN hands
    out. An IP literal cannot be rebound - the browser never resolves it - and
    the reserved suffixes cannot be registered on the public internet.
    """
    async with client_for_host(hostname) as http:
        assert (await http.get("/api/health")).status_code == 200


@pytest.mark.asyncio
async def test_a_hostname_can_be_allowed_by_configuration(settings: Settings) -> None:
    """An escape hatch for anyone fronting this with a real name."""
    settings.allowed_hosts = ["files.example.com"]
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, client=LOOPBACK_CLIENT)
        async with AsyncClient(transport=transport, base_url="http://files.example.com") as http:
            assert (await http.get("/api/health")).status_code == 200
        async with AsyncClient(transport=transport, base_url="http://other.example.com") as http:
            assert (await http.get("/api/health")).status_code == 400


def test_the_websocket_refuses_an_outside_hostname(settings: Settings) -> None:
    """The socket carries events and must not be reachable by a rebound page.

    Checked separately because middleware that only handles `http` scopes
    leaves the WebSocket wide open, which is the easy mistake here.
    """
    application = create_app(settings)
    with (
        TestClient(application) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/ws?device_id=" + str(uuid.uuid4()),
            headers={"Host": "evil.example.com"},
        ),
    ):
        pass


# -- two uploads of one chunk --------------------------------------------------


@pytest.mark.asyncio
async def test_two_uploads_of_one_chunk_do_not_corrupt_it(client, sender, receiver) -> None:
    """A retry can overlap the attempt it is replacing.

    `sendChunk` retries an index after a stall, but a stalled request is not a
    finished one: the server may still be reading the first body when the
    second arrives. Both wrote to the same `<index>.tmp` staging file. On
    Windows the second open failed outright - the client saw a 500 - and on a
    POSIX host the two writes interleave into a chunk of the right length and
    the wrong contents, which assembles into a corrupt file that nothing
    downstream checks.

    Re-sending a chunk is documented as safe, so the rule is: whichever write
    wins, the chunk is wholly one of them.
    """
    size = 40_000
    created = await client.post(
        "/api/transfers",
        json={"filename": "race.bin", "size": size, "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = created.json()["transfer_id"]

    async def put(fill: bytes):
        return await client.put(
            f"/api/transfers/{transfer_id}/chunks/0",
            content=fill * size,
            headers=headers(sender),
        )

    responses = await asyncio.gather(put(b"A"), put(b"B"))
    assert [r.status_code for r in responses] == [200, 200]

    completed = await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    assert completed.status_code == 200

    download = await client.get(f"/api/files/{transfer_id}/download", headers=headers(receiver))
    assert download.content in (b"A" * size, b"B" * size)


# -- a media type the HTTP layer cannot send -----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime_type",
    ["text/html\r\nX-Injected: yes", "not a media type", "text/html\nX-Injected: yes", "/", "a/"],
)
async def test_a_media_type_the_server_cannot_send_is_refused(
    client, sender, receiver, mime_type
) -> None:
    """The sender chose this string and the download echoes it back.

    uvicorn refuses to put a CR or LF in a header value - correctly - but it
    refuses at send time, so the transfer uploads and completes and then every
    download of it dies with "Invalid HTTP header value" and a traceback. The
    receiver can never fetch that file, and only the sender can un-poison it.
    Reject the value where it arrives instead.
    """
    response = await client.post(
        "/api/transfers",
        json={
            "filename": "x.bin",
            "size": 3,
            "receiver_id": receiver,
            "mime_type": mime_type,
        },
        headers=headers(sender),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime_type",
    ["image/jpeg", "video/quicktime", "text/plain; charset=utf-8", "application/octet-stream"],
)
async def test_the_media_types_browsers_send_are_accepted(
    client, sender, receiver, mime_type
) -> None:
    """A validator that rejects real files would be worse than the bug."""
    response = await client.post(
        "/api/transfers",
        json={"filename": "x.bin", "size": 3, "receiver_id": receiver, "mime_type": mime_type},
        headers=headers(sender),
    )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_a_stored_media_type_that_cannot_be_sent_falls_back(
    app, client, sender, receiver
) -> None:
    """Rows written before the check exists must still be downloadable.

    Validation at the door does nothing for a transfer already in the database,
    so the download refuses to hand a broken value to the HTTP layer.
    """
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="legacy.bin",
        payload=b"hello",
    )
    await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))

    connection = app.state.database.connection
    await connection.execute(
        "UPDATE transfers SET mime_type = ? WHERE id = ?",
        ("text/html\r\nX-Injected: yes", transfer_id),
    )
    await connection.commit()

    download = await client.get(f"/api/files/{transfer_id}/download", headers=headers(receiver))
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/octet-stream")


# -- chunks of transfers that no longer exist ----------------------------------


@pytest.mark.asyncio
async def test_startup_removes_chunks_no_transfer_can_claim(
    app, client, sender, receiver, settings: Settings
) -> None:
    """Half-finished uploads used to keep their chunks for ever.

    A transfer that is cancelled cleans up after itself, but a browser that is
    closed mid-upload never cancels anything: the row stays `uploading` and the
    partial chunks sit on disk until someone notices. This server had thirteen
    such directories, several with no row at all.

    Startup is the one moment when nothing can be in flight, so it is the safe
    place to sweep. A row that could still be resumed is left alone.
    """
    active = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="resumable.bin",
        payload=b"x" * 200_000,
        skip={1},
    )

    finished = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="done.bin",
        payload=b"y" * 1000,
    )
    await client.post(f"/api/transfers/{finished}/complete", headers=headers(sender))
    # Completion already removes these; put them back to prove the sweep, not
    # the thing that was already working.
    (settings.temporary_dir / finished).mkdir(parents=True, exist_ok=True)
    (settings.temporary_dir / finished / "0.part").write_bytes(b"left over")

    orphan = str(uuid.uuid4())
    (settings.temporary_dir / orphan).mkdir(parents=True, exist_ok=True)
    (settings.temporary_dir / orphan / "0.part").write_bytes(b"no row at all")

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        pass

    assert not (settings.temporary_dir / orphan).exists(), "a chunk directory with no row"
    assert not (settings.temporary_dir / finished).exists(), "chunks of a completed transfer"
    assert (settings.temporary_dir / active).is_dir(), "an upload that can still be resumed"


@pytest.mark.asyncio
async def test_the_sweep_leaves_anything_it_cannot_identify(settings: Settings) -> None:
    """Deleting is destructive, so the rule is: only what is provably ours."""
    settings.ensure_directories()
    stray = settings.temporary_dir / "not-a-transfer-id.txt"
    stray.write_text("someone else's file", encoding="utf-8")

    application = create_app(settings)
    async with application.router.lifespan_context(application):
        pass

    assert stray.exists()


@pytest.mark.asyncio
async def test_an_upload_nobody_can_finish_is_given_up_on(
    app, client, sender, receiver, settings: Settings
) -> None:
    """The conservative sweep alone did not fix the leak it was written for.

    On the machine this was found on, eleven of twelve chunk directories
    belonged to rows still marked `uploading` - abandoned days earlier during
    debugging. Resume lives in the page: the browser holds the transfer id in
    memory and never writes it down, so once the tab is closed the upload is
    unreachable and its chunks are dead weight. Keeping them for ever is not
    caution, it is a disk leak with a good excuse.
    """
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="abandoned.bin",
        payload=b"x" * 200_000,
        skip={1},
    )
    connection = app.state.database.connection
    await connection.execute(
        "UPDATE transfers SET created_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", transfer_id),
    )
    await connection.commit()
    assert (settings.temporary_dir / transfer_id).is_dir()

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        status = await TransferRepository.get(restarted.state.database.connection, transfer_id)

    assert status is not None
    assert status["status"] == "failed"
    assert status["error"] == "Abandoned before it finished"
    assert not (settings.temporary_dir / transfer_id).exists()


@pytest.mark.asyncio
async def test_an_upload_from_moments_ago_survives_a_restart(
    app, client, sender, receiver, settings: Settings
) -> None:
    """The window exists for one case: the server restarting under an open page.

    That is not hypothetical - it happened twice while this project was being
    debugged, and the page did resume afterwards.
    """
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="in-progress.bin",
        payload=b"x" * 200_000,
        skip={1},
    )

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        status = await TransferRepository.get(restarted.state.database.connection, transfer_id)

    assert status is not None
    assert status["status"] == "uploading"
    assert (settings.temporary_dir / transfer_id).is_dir()


# -- two registrations of one device id ----------------------------------------


@pytest.mark.asyncio
async def test_registering_one_new_id_twice_at_once_is_refused_cleanly(client) -> None:
    """A double-submit used to escape as an unhandled IntegrityError.

    Both requests found no such device, both inserted, and the loser's
    `UNIQUE constraint failed: devices.id` propagated out of the endpoint as a
    500 with a traceback. The answer is the same one a slower duplicate already
    gets: that id is taken.
    """
    payload = {"device_id": str(uuid.uuid4()), "name": "Twin", "user_agent": "x"}
    responses = await asyncio.gather(
        client.post("/api/devices/register", json=payload),
        client.post("/api/devices/register", json=payload),
    )

    codes = sorted(response.status_code for response in responses)
    assert codes == [200, 403], [r.text for r in responses]


@pytest.mark.asyncio
async def test_clearing_history_does_not_block_the_event_loop(app, client, sender) -> None:
    """Sanity check on the cleanup path clear-history runs.

    Not a timing assertion - those are flaky - just that clearing a history
    with chunk directories behind it still succeeds and removes them.
    """
    receiver_id = await register(client, "Other")
    service = app.state.transfer_service
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver_id,
        filename="partial.bin",
        payload=b"z" * 1000,
    )
    await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    directory = Path(service._storage.transfer_dir(transfer_id))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "0.part").write_bytes(b"left over")

    cleared = await client.delete("/api/transfers", headers=headers(sender))
    assert cleared.status_code == 200
    assert cleared.json()["deleted"] == 1
    assert not directory.exists()
    assert await TransferRepository.get(app.state.database.connection, transfer_id) is None
