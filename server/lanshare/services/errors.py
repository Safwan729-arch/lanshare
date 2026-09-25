"""Service-level errors, mapped to HTTP responses by a handler in main.py.

Keeps the services free of FastAPI imports while still letting routers stay thin.
"""

from __future__ import annotations


class LanShareError(Exception):
    """Base class. ``status_code`` is what the client will see."""

    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class BadRequest(LanShareError):
    status_code = 400


class Unauthorized(LanShareError):
    """Who you are could not be established."""

    status_code = 401


class Forbidden(LanShareError):
    status_code = 403


class NotFound(LanShareError):
    status_code = 404


class Conflict(LanShareError):
    status_code = 409


class PayloadTooLarge(LanShareError):
    status_code = 413


class TooManyRequests(LanShareError):
    status_code = 429
