"""Filename sanitizing and collision handling - the parts that touch the OS."""

from __future__ import annotations

from pathlib import Path

import pytest
from lanshare.services.storage import (
    Storage,
    StorageError,
    sanitize_filename,
    unique_destination,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("photo.jpg", "photo.jpg"),
        ("../../etc/passwd", "passwd"),
        (r"..\..\windows\system32\config", "config"),
        ("C:/Users/me/secret.txt", "secret.txt"),
        ("with/slash.txt", "slash.txt"),
        ('bad<>:"|?*chars.txt', "bad_______chars.txt"),
        ("trailing.dots...", "trailing.dots"),
        ("   spaced.txt   ", "spaced.txt"),
        ("CON", "_CON"),
        # Windows reserves these case-insensitively and whatever the extension.
        ("nul.txt", "_nul.txt"),
        ("NUL.txt", "_NUL.txt"),
        ("COM1.log", "_COM1.log"),
        ("", "file"),
        ("...", "file"),
        ("/", "file"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_strips_control_characters() -> None:
    assert sanitize_filename("note\x00\x1f.txt") == "note__.txt"


def test_sanitize_keeps_unicode() -> None:
    # Non-ASCII names are legal on Windows; only the illegal set is replaced.
    assert sanitize_filename("photo-café-😀.jpg") == "photo-café-😀.jpg"


def test_sanitize_caps_length_but_keeps_extension() -> None:
    result = sanitize_filename("a" * 400 + ".jpeg")
    assert len(result) <= 180
    assert result.endswith(".jpeg")


def test_unique_destination_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "photo.jpg").write_bytes(b"first")
    assert unique_destination(tmp_path, "photo.jpg").name == "photo (1).jpg"

    (tmp_path / "photo (1).jpg").write_bytes(b"second")
    assert unique_destination(tmp_path, "photo.jpg").name == "photo (2).jpg"


def test_transfer_dir_rejects_traversal(tmp_path: Path) -> None:
    storage = Storage(incoming_dir=tmp_path / "in", temporary_dir=tmp_path / "tmp")
    with pytest.raises(StorageError):
        storage.transfer_dir("../escape")


def test_chunk_path_rejects_negative_index(tmp_path: Path) -> None:
    storage = Storage(incoming_dir=tmp_path / "in", temporary_dir=tmp_path / "tmp")
    with pytest.raises(StorageError):
        storage.chunk_path("11111111-2222-3333-4444-555555555555", -1)
