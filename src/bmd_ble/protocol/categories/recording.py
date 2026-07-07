"""
bmd_ble/protocol/categories/recording.py
=========================================
Recording category — record start / stop command encoding and echo decoding.

WHAT BELONGS HERE
------------------
Only the encode/decode logic for the recording command family (assembling
a BMD command packet for a record start/stop request, and reading back an
``INCOMING_CONTROL`` echo to confirm it). The category byte, parameter byte,
payload data type, reserved byte, and payload value are model/firmware-
specific and must be supplied by the caller from a ``CameraProfile`` — see
camera_profile.py. They are never hardcoded here (CLAUDE.md design
principles 1 and 6).

STATUS
------
Category/parameter/data_type/payload values for ``POCKET_6K_G2 v7.9`` are
reverse-engineered and byte-level cross-validated against a real captured
command (see ``payloads/models/POCKET_6K_G2_v7.9.json`` and
``docs/recording.md``) — real hardware does not use a plain boolean 0/1
payload; record start is ``2``, stop is ``0``. No live hardware round-trip
(echo) has confirmed these yet, so the profile stays ``UNVERIFIED``. See
docs/recording.md for the open question and remaining verification work.
"""

from __future__ import annotations

import struct

from ..codec import DESTINATION_CAMERA, RESERVED_BYTE, CommandHeader, Operation, encode_packet
from ..types import DATA_TYPE_BYTE_WIDTHS, DATA_TYPE_STRUCT_FORMATS, DataType


def _encode_recording_state(
    category: int, parameter: int, data_type: DataType, value: int, reserved: int
) -> bytes:
    if data_type not in DATA_TYPE_STRUCT_FORMATS:
        raise ValueError(f"Unsupported data type for recording state payload: {data_type!r}")

    header = CommandHeader(
        destination=DESTINATION_CAMERA,
        command_id=0x00,
        category=category,
        parameter=parameter,
        data_type=data_type,
        operation=Operation.ASSIGN,
        reserved=reserved,
    )
    width = DATA_TYPE_BYTE_WIDTHS[data_type]
    payload = value.to_bytes(width, byteorder="little", signed=value < 0)
    return encode_packet(header, payload)


def encode_record_start(
    *,
    category: int,
    parameter: int,
    data_type: DataType,
    value: int,
    reserved: int = RESERVED_BYTE,
) -> bytes:
    """Encode a record-start command packet.

    ``category``, ``parameter``, ``data_type``, ``value``, and ``reserved``
    must come from ``CameraProfile`` for the target camera/firmware — never
    invented. Real hardware payload values are not plain booleans (e.g.
    POCKET_6K_G2 v7.9 uses ``2``, not ``1``).
    """
    return _encode_recording_state(category, parameter, data_type, value, reserved)


def encode_record_stop(
    *,
    category: int,
    parameter: int,
    data_type: DataType,
    value: int,
    reserved: int = RESERVED_BYTE,
) -> bytes:
    """Encode a record-stop command packet.

    ``category``, ``parameter``, ``data_type``, ``value``, and ``reserved``
    must come from ``CameraProfile`` for the target camera/firmware — never
    invented.
    """
    return _encode_recording_state(category, parameter, data_type, value, reserved)


def is_recording_state_echo(header: CommandHeader, *, category: int, parameter: int) -> bool:
    """Whether a decoded packet header matches the recording state category/parameter.

    Used to identify the ``INCOMING_CONTROL`` echo for a record start/stop
    command before decoding its payload — see CLAUDE.md's verification
    strategy (echo first, ``CAMERA_STATUS`` as secondary cross-check).
    """
    return header.category == category and header.parameter == parameter


def decode_recording_state(payload: bytes, data_type: DataType) -> bool:
    """Decode a recording state echo payload into a recording/not-recording bool.

    Real hardware payload values aren't plain 0/1 (POCKET_6K_G2 v7.9 uses
    2/0), but nonzero-vs-zero truthiness still correctly distinguishes
    recording from stopped, so no value beyond True/False is needed here.
    """
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
