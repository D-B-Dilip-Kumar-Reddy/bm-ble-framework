"""
bmd_camera/rest/exceptions.py
===============================
Re-exports the transport-neutral exception types from bmd_camera.exceptions
(BMDConnectionError, BMDTimeoutError, BMDVerificationError,
BMDUnsupportedError, BMDStorageError all apply unchanged to REST — none of
their names are BLE-specific) and adds the one REST-only type.
"""

from __future__ import annotations

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
    """
