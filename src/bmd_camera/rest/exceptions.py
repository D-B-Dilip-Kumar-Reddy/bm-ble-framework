"""
bmd_camera/rest/exceptions.py
===============================
Re-exports the transport-neutral exception types from bmd_camera.exceptions
(BMDConnectionError, BMDTimeoutError, BMDVerificationError,
BMDUnsupportedError, BMDStorageError all apply unchanged to REST — none of
their names are BLE-specific) and adds the one REST-only type.
"""

from __future__ import annotations

from typing import Any

from ..exceptions import (
    BMDConnectionError,
    BMDStorageError,
    BMDTimeoutError,
    BMDUnsupportedError,
    BMDVerificationError,
)

__all__ = [
    "BMDConnectionError",
    "BMDRestError",
    "BMDStorageError",
    "BMDTimeoutError",
    "BMDUnsupportedError",
    "BMDVerificationError",
]


class BMDRestError(Exception):
    """Raised for a REST request that failed for a reason other than "not
    implemented" — a non-2xx, non-501 status, or a malformed response body.

    `501` raises BMDUnsupportedError instead (design principle 7 — the
    camera is correctly declining an operation it doesn't support, not
    failing). See RestClient's status-handling contract in
    docs/rest/transport.md.

    Carries the raw `status` and parsed `body` as attributes (not just
    embedded in the message string) so a caller with enough camera-semantic
    context can interpret a specific response meaningfully — e.g.
    `RestCameraSession.clips()` re-raising as `BMDStorageError` when
    `/clips/list` 404s with `{"error": "No disk or media"}` (real-hardware-
    confirmed, `POCKET_6K_G2 v8.6`, 2026-08-03) — without string-matching
    the message.
    """

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
