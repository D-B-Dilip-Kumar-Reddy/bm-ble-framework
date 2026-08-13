"""
bmd_camera/ble/protocol/categories/media.py
=========================================
Media category — photo capture trigger encoding.

WHAT BELONGS HERE
------------------
Only the encode logic for the still-capture trigger (SDI 10.3, VOID). The
category byte, parameter byte, and reserved byte are model/firmware-specific
and must be supplied by the caller from a ``CameraProfile`` — never
hardcoded here (CLAUDE.md design principles 1 and 6).

STATUS
------
Confirmed independently on two cameras (``docs/ble/photo_capture.md`` §7,
§9): a void (payloadless) ``ASSIGN`` to this coordinate reliably produces a
new file on the SD card, with the reserved byte confirmed indifferent
(``0x00``/``0x01`` both work) and **zero ``INCOMING_CONTROL`` footprint
either time** — no echo, no ``CAMERA_STATUS`` movement. There is therefore
no decode/echo-matching function in this module to pair with the encoder,
unlike every other category here: none exists on the wire to decode. See
``CameraSession.capture_photo()`` for how this asymmetry is handled at the
session layer.
"""

from __future__ import annotations

from ..codec import RESERVED_BYTE, encode_assign_void


def encode_photo_trigger(*, category: int, parameter: int, reserved: int = RESERVED_BYTE) -> bytes:
    """Encode the photo-capture trigger command packet.

    ``category``, ``parameter``, and ``reserved`` must come from
    ``CameraProfile`` for the target camera/firmware — never invented. A
    void (payloadless) ``ASSIGN``; see ``docs/ble/photo_capture.md`` §7.1 for
    why no value is carried and why the reserved byte is confirmed
    indifferent on both cameras tested so far.
    """
    return encode_assign_void(category=category, parameter=parameter, reserved=reserved)
