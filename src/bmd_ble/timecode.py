"""
bmd_ble/timecode.py
=====================
TIMECODE characteristic decode and clip-duration math.

TIMECODE notifications are NOT a bare 32-bit BCD value directly on the
characteristic, despite constants.py's "HH:MM:SS:mm" doc comment (that
comment describes the human-readable field order, not the wire encoding).
Real captures on both POCKET_6K_G2 v7.9 and POCKET_6K_PRO v8.6 show TIMECODE
notifications are full BMD-style packets — the same header shape as
OUTGOING_CONTROL/INCOMING_CONTROL (see protocol/codec.py) — wrapping a 4-byte
BCD payload. This module reuses protocol.codec.decode_packet to unwrap that
header rather than hand-rolling a second parser, but still lives alongside
session.py/camera_controller.py rather than under protocol/categories/:
TIMECODE isn't a general SDI command sent/echoed through OUTGOING_CONTROL —
it's a single fixed reading pushed by a distinct BLE characteristic.

Wire format (sniffer-verified on both cameras above — every TIMECODE
notification captured so far uses this exact header):
    destination=0xFF, reserved=0xFF, category=0x09, parameter=0x04,
    data_type=INT32, operation=ASSIGN, payload=4 bytes BCD, byte order
    [frames, seconds, minutes, hours] — least-significant field first,
    the reverse of the constants.py "HH:MM:SS:mm" doc order.

STATUS: the header signature and BCD field order above are confirmed by real
capture. The `frames` field has only been observed cycling 0-23 (rolling
over into `seconds`) in the captures seen so far — consistent with a 24fps
frame counter, but not confirmed to be fps-independent across other frame
rates. See docs/timecode.md. duration_seconds() therefore only uses
hours/minutes/seconds; `frames` is decoded and available on Timecode for
display/reference, but excluded from duration math until its rollover
semantics are confirmed across more frame rates.
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol.codec import decode_packet
from .protocol.types import DataType

# TIMECODE characteristic wire header — sniffer-verified identical on both
# POCKET_6K_G2 v7.9 and POCKET_6K_PRO v8.6 captures. Unlike OUTGOING_CONTROL
# command category/parameter pairs, this isn't treated as per-model profile
# data: it's the same on both cameras sniffed so far, matching how
# protocol/codec.py's Operation values are spec/characteristic-level
# constants rather than per-profile ones. Revisit if a future camera model
# is sniffed with a different TIMECODE header.
TIMECODE_CATEGORY = 0x09
TIMECODE_PARAMETER = 0x04


@dataclass(frozen=True)
class Timecode:
    """Decoded TIMECODE reading. `frames` rollover semantics are unconfirmed — see module docstring."""  # noqa: E501

    hours: int
    minutes: int
    seconds: int
    frames: int


def _bcd_byte(byte: int) -> int:
    """Decode one BCD byte (two decimal digits packed into a nibble each)."""
    return (byte >> 4) * 10 + (byte & 0x0F)


def decode_timecode(data: bytes) -> Timecode:
    """Decode a TIMECODE notification (a full wrapped BMD packet, not a bare BCD value).

    Raises ValueError if the packet doesn't decode, doesn't match the
    sniffer-verified TIMECODE header signature (category/parameter/data
    type), or the payload isn't 4 bytes.
    """
    header, payload = decode_packet(data)

    if (header.category, header.parameter) != (TIMECODE_CATEGORY, TIMECODE_PARAMETER):
        raise ValueError(
            f"Not a TIMECODE packet: category=0x{header.category:02X} "
            f"parameter=0x{header.parameter:02X}"
        )
    if header.data_type != DataType.INT32:
        raise ValueError(f"Unexpected TIMECODE data type: {header.data_type!r}")
    if len(payload) != 4:
        raise ValueError(f"Expected a 4-byte TIMECODE payload, got {len(payload)} bytes")

    frames, seconds, minutes, hours = (_bcd_byte(b) for b in payload)
    return Timecode(hours=hours, minutes=minutes, seconds=seconds, frames=frames)


def duration_seconds(start: Timecode, stop: Timecode) -> float:
    """Elapsed seconds between two TIMECODE readings, from hours/minutes/seconds only.

    `frames` is intentionally excluded: correctly carrying it into whole
    seconds requires knowing its rollover point across frame rates, which
    isn't confirmed yet (see module docstring). Raises ValueError if `stop`
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
