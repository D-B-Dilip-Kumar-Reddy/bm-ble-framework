"""
bmd_camera/rest/timecode.py
=============================
REST TIMECODE decode. `GET /transports/0/timecode` (and the matching
`propertyValueChanged` event) returns `{"timecode": <int>, "clip": <int>}`,
where `timecode` is BCD-packed HH:MM:SS:FF, big-endian — the OPPOSITE byte
order from the BLE `TIMECODE` characteristic's `[frames, seconds, minutes,
hours]` (see docs/ble/timecode.md and docs/rest/transport.md's "Timecode is
BCD, big-endian, and byte-reversed relative to BLE").

Confirmed on real `POCKET_6K_G2 v8.6` hardware (2026-08-03,
docs/rest/transport.md): `{"timecode": 274153986}` == `0x10574202` ==
`10:57:42:02`, matching the time-of-day the sweep actually ran at.

The `Timecode` dataclass and `duration_seconds()` are reused as-is from
`bmd_camera.ble.timecode` — only the wire decode differs; the resulting
value and the clip-duration math are transport-agnostic. `clip` (a second
BCD-packed timecode, the position within the current clip rather than
time-of-day) is not yet decoded — no confirmed use for it yet, and its
field is otherwise unused.
"""

from __future__ import annotations

from ..ble.timecode import Timecode

__all__ = ["Timecode", "decode_rest_timecode"]


def _bcd_byte(byte: int) -> int:
    """Decode one BCD byte (two decimal digits packed into a nibble each)."""
    return (byte >> 4) * 10 + (byte & 0x0F)


def decode_rest_timecode(raw: int) -> Timecode:
    """Decode the `timecode` field of `GET /transports/0/timecode`'s body
    (or a `/transports/0/timecode` `propertyValueChanged` event's `value`):
    BCD HH:MM:SS:FF packed big-endian into a 32-bit integer."""
    hours = _bcd_byte((raw >> 24) & 0xFF)
    minutes = _bcd_byte((raw >> 16) & 0xFF)
    seconds = _bcd_byte((raw >> 8) & 0xFF)
    frames = _bcd_byte(raw & 0xFF)
    return Timecode(hours=hours, minutes=minutes, seconds=seconds, frames=frames)
