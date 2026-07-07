"""
bmd_ble/exceptions.py
======================
Exception types raised across the bmd_ble package.
"""

from __future__ import annotations


class BMDConnectionError(Exception):
    """Raised when connecting to or maintaining a BLE connection fails."""


class BMDTimeoutError(Exception):
    """Raised when an operation exceeds its configured timeout."""


class BMDCommandError(Exception):
    """Raised when a command cannot be constructed or sent."""


class BMDVerificationError(Exception):
    """Raised when a write command cannot be confirmed via echo or status."""


class BMDUnsupportedError(Exception):
    """Raised when an operation is attempted that the camera profile doesn't support."""


class BMDStorageError(Exception):
    """Raised when storage media is missing, full, or in an error state."""
