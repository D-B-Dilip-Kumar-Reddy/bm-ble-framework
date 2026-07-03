"""
bmd_ble/protocol/categories/recording.py
=========================================
Recording category — record start / stop command encoding and echo decoding.

WHAT BELONGS HERE
------------------
Only the encode/decode logic for the recording command family (assembling
a BMD command packet for a record start/stop request, and reading back an
``INCOMING_CONTROL`` echo to confirm it). The category byte, parameter byte,
and payload data type are model/firmware-specific and must be supplied by
the caller from a sniffer-verified ``CameraProfile`` — see camera_profile.py.
They are never hardcoded here (CLAUDE.md design principle 1 and 6).

STATUS
------
Scaffold only. No sniffer capture has yet confirmed the recording category,
parameter, or payload data type for any camera in this repo. See
docs/recording.md for the verification workflow and current gap. Do not
call these functions with invented category/parameter values, and do not
mark a profile's recording fields VERIFIED until real hardware confirms
them (CLAUDE.md, "Workflow: Adding a New Command").
"""

from __future__ import annotations

import struct

from ..codec import CommandHeader, Operation, encode_packet
from ..types import DATA_TYPE_BYTE_WIDTHS, DATA_TYPE_STRUCT_FORMATS, DataType


def _encode_recording_state(
    category: int, parameter: int, data_type: DataType, recording: bool
) -> bytes:
    fmt = DATA_TYPE_STRUCT_FORMATS.get(data_type)
    if fmt is None:
        raise ValueError(f"Unsupported data type for recording state payload: {data_type!r}")

    header = CommandHeader(
        destination=0x00,
        command_id=0x00,
        category=category,
        parameter=parameter,
        data_type=data_type,
        operation=Operation.ASSIGN,
    )
    payload = struct.pack(f"<{fmt}", recording)
    return encode_packet(header, payload)


def encode_record_start(*, category: int, parameter: int, data_type: DataType) -> bytes:
    """Encode a record-start command packet.

    ``category``, ``parameter``, and ``data_type`` must come from
    ``CameraProfile`` for the target camera/firmware — never invented.
    """
    return _encode_recording_state(category, parameter, data_type, recording=True)


def encode_record_stop(*, category: int, parameter: int, data_type: DataType) -> bytes:
    """Encode a record-stop command packet.

    ``category``, ``parameter``, and ``data_type`` must come from
    ``CameraProfile`` for the target camera/firmware — never invented.
    """
    return _encode_recording_state(category, parameter, data_type, recording=False)


def is_recording_state_echo(header: CommandHeader, *, category: int, parameter: int) -> bool:
    """Whether a decoded packet header matches the recording state category/parameter.

    Used to identify the ``INCOMING_CONTROL`` echo for a record start/stop
    command before decoding its payload — see CLAUDE.md's verification
    strategy (echo first, ``CAMERA_STATUS`` as secondary cross-check).
    """
    return header.category == category and header.parameter == parameter


def decode_recording_state(payload: bytes, data_type: DataType) -> bool:
    """Decode a recording state echo payload into a recording/not-recording bool."""
    fmt = DATA_TYPE_STRUCT_FORMATS.get(data_type)
    if fmt is None:
        raise ValueError(f"Unsupported data type for recording state payload: {data_type!r}")

    width = DATA_TYPE_BYTE_WIDTHS[data_type]
    if len(payload) != width:
        raise ValueError(
            f"Expected {width}-byte payload for recording state, got {len(payload)} bytes"
        )
    (value,) = struct.unpack(f"<{fmt}", payload)
    return bool(value)
