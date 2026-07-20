"""
bmd_ble/protocol/categories/storage.py
========================================
Storage category — passive decode of camera-originated storage-monitoring
notifications.

WHAT BELONGS HERE
------------------
Unlike ``recording.py``, nothing here is ever sent by this repo's code —
CLAUDE.md design principle 10's storage monitoring (card ready, remaining
capacity, ...) is still *(planned)* and unimplemented; this module only
decodes unsolicited ``INCOMING_CONTROL`` reports. Category, parameter, data
type, and the meaningful payload byte offset are all supplied by the caller
from a ``CameraProfile``'s ``storage`` block — never hardcoded here
(CLAUDE.md design principles 1 and 6).

STATUS
------
Only one signal is modeled so far: a CANDIDATE "write margin" warning
(category ``0x09``, parameter ``0x01``) observed to precede a
camera-initiated recording stop on a known-slow SD card — see
``docs/recording.md``'s "Camera-initiated stop detection" section for the
full evidence and CANDIDATE framing. It is not yet isolated from other
possible autostop causes (card full, card removed, power loss).
"""

from __future__ import annotations

import struct

from ..codec import CommandHeader, header_matches
from ..types import DATA_TYPE_BYTE_WIDTHS, DATA_TYPE_STRUCT_FORMATS, DataType


def is_storage_notification(header: CommandHeader, *, category: int, parameter: int) -> bool:
    """Whether a decoded packet header matches a storage-category signal."""
    return header_matches(header, category=category, parameter=parameter)


def decode_write_margin(payload: bytes, data_type: DataType, *, byte_offset: int) -> int:
    """Decode the signed value at ``byte_offset`` within a storage notification payload.

    Returns the raw decoded integer — callers compare it against the
    profile's named ``values`` (e.g. ``spec.values["low_margin"]``), not a
    sign-based or range-based heuristic: only the exact values actually seen
    in captures so far are meaningful (CLAUDE.md design principle 6 — never
    generalize beyond what's been observed on the wire).

    ``byte_offset`` has no default here: unlike ``decode_recording_state``
    (which always reads from the start of the payload), this signal's
    meaningful byte was observed at offset 1, not 0 — offsets 0 and 2 are
    constant across every capture seen so far and not understood. The offset
    is itself a sniffer-observed fact and must always come from the profile.
    """
    fmt = DATA_TYPE_STRUCT_FORMATS.get(data_type)
    if fmt is None:
        raise ValueError(f"Unsupported data type for storage signal payload: {data_type!r}")

    width = DATA_TYPE_BYTE_WIDTHS[data_type]
    end = byte_offset + width
    if len(payload) < end:
        raise ValueError(
            f"Expected at least {end}-byte payload for storage signal "
            f"(byte_offset={byte_offset}, width={width}), got {len(payload)} bytes"
        )
    (value,) = struct.unpack(f"<{fmt}", payload[byte_offset:end])
    return value
