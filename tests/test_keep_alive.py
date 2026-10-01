"""The idle connection window.

Uvicorn closes an idle keep-alive connection after 5 seconds by default. A phone
keeps that socket in its pool and reuses it; a `POST` on the dead socket is not
retried by the browser, because a POST is not safe to repeat. The upload then
hangs with no error at all.

Five seconds is shorter than the time it takes a person to pick a video out of
their gallery, which is exactly why some transfers started and others never did.
"""

from __future__ import annotations

import inspect

import uvicorn
from lanshare.__main__ import main
from lanshare.config import Settings


def test_the_idle_window_outlasts_picking_a_file() -> None:
    """A human browsing a gallery must not outlive the connection."""
    assert Settings().keep_alive_timeout >= 60, (
        "an idle window this short closes the connection while the user is still "
        "choosing a file; their next upload then hangs with no error"
    )


def test_the_default_is_not_uvicorns() -> None:
    """Guards against the setting existing but never being raised."""
    uvicorn_default = (
        inspect.signature(uvicorn.config.Config.__init__).parameters["timeout_keep_alive"].default
    )
    assert Settings().keep_alive_timeout > uvicorn_default


def test_the_setting_actually_reaches_uvicorn(monkeypatch) -> None:
    """A setting the server does not pass on is worse than no setting."""
    captured: dict[str, object] = {}

    def fake_run(_app: str, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    main()

    assert captured.get("timeout_keep_alive") == Settings().keep_alive_timeout
