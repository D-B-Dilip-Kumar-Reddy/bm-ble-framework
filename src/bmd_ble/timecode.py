"""
bmd_ble/timecode.py
=====================
TIMECODE characteristic decode and clip-duration math.

Not a BMD command packet — TIMECODE is a distinct BLE characteristic with
its own 32-bit BCD encoding (constants.py: "HH:MM:SS:mm", e.g.
09:12:53:10 = 0x09125310), so this lives alongside session.py/
camera_controller.py rather than under protocol/ (which is BMD packet
encoding/decoding only — see CLAUDE.md design principle 5).

STATUS: the nibble layout (4 BCD digit-pairs) is a fixed, mechanical
encoding and safe to decode as documented. The *meaning* of the 4th field
(video frames? milliseconds? something else) and its rollover point are
NOT yet confirmed against a real capture — see docs/timecode.md.
duration_seconds() therefore only uses hours/minutes/seconds; the 4th field
is decoded and available on Timecode for display/reference, but excluded
from duration math until its semantics are confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Timecode:
    """Decoded TIMECODE reading. `subfield` meaning is unconfirmed — see module docstring."""

    hours: int
    minutes: int
    seconds: int
    subfield: int


def _bcd_byte(byte: int) -> int:
    """Decode one BCD byte (two decimal digits packed into a nibble each)."""
    return (byte >> 4) * 10 + (byte & 0x0F)


def decode_timecode(data: bytes) -> Timecode:
    """Decode the 32-bit BCD TIMECODE value into its four fields.

    Byte order matches the documented example: 0x09125310 == 09:12:53:10,
    i.e. bytes are [hours, minutes, seconds, subfield] in that order.
    """
    if len(data) != 4:
        raise ValueError(f"Expected a 4-byte TIMECODE value, got {len(data)} bytes")

    hours, minutes, seconds, subfield = (_bcd_byte(b) for b in data)
    return Timecode(hours=hours, minutes=minutes, seconds=seconds, subfield=subfield)


def duration_seconds(start: Timecode, stop: Timecode) -> float:
    """Elapsed seconds between two TIMECODE readings, from hours/minutes/seconds only.

    The subfield (frames vs milliseconds — unconfirmed) is intentionally
    excluded: correctly carrying it into whole seconds requires knowing its
    rollover point, which isn't confirmed yet. Raises ValueError if `stop`
    is not strictly after `start` — no midnight/24h rollover handling.
    """
    start_total = start.hours * 3600 + start.minutes * 60 + start.seconds
    stop_total = stop.hours * 3600 + stop.minutes * 60 + stop.seconds
    if stop_total <= start_total:
        raise ValueError(
            f"stop timecode ({stop.hours:02d}:{stop.minutes:02d}:{stop.seconds:02d}) "
            f"is not after start timecode "
            f"({start.hours:02d}:{start.minutes:02d}:{start.seconds:02d})"
        )
    return float(stop_total - start_total)
