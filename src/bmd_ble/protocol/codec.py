"""
bmd_ble/protocol/codec.py
=========================
BMD command packet header encode / decode.

Packet structure (see CLAUDE.md):

    Byte 0      Fixed prefix byte (0xFF — sniffer-verified on POCKET_6K_G2
                v7.9, both directions; not a per-destination address)
    Byte 1      Length field — counts only bytes 4 onwards (category,
                parameter, data type, operation, payload). Sniffer-verified:
                it does NOT count command_id/reserved, unlike the generic
                BMD spec's "length of everything after byte 1" assumption.
    Byte 2      Command ID / type
    Byte 3      Reserved
    Byte 4      Category
    Byte 5      Parameter
    Byte 6      Data type
    Byte 7      Operation  (0x00 = assign, 0x01 = offset, 0x02 = camera
                report — sniffer-verified on every camera-originated
                notification captured so far; official spec meaning
                unconfirmed)
    Bytes 8+    Payload

This module only knows about the header. It has no knowledge of what a
particular category/parameter pair means — that lives in
protocol/categories/<category>.py once confirmed by a sniffer capture.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

from .types import DATA_TYPE_BYTE_WIDTHS, DATA_TYPE_STRUCT_FORMATS, DataType

DESTINATION_CAMERA = 0xFF
RESERVED_BYTE = 0x00

HEADER_LENGTH = 8

# Leading bytes NOT counted by the length field (byte 1): the prefix byte
# itself, the length byte, command_id, and reserved. Sniffer-verified on
# POCKET_6K_G2 v7.9: actual_total_length == declared_length + LENGTH_FIELD_OFFSET
# in every captured packet, including known record start/stop commands.
LENGTH_FIELD_OFFSET = 4


class Operation(IntEnum):
    """BMD packet operation identifier (packet header byte 7)."""

    ASSIGN = 0x00
    OFFSET = 0x01
    # Sniffer-verified on POCKET_6K_G2 v7.9: every camera-originated
    # INCOMING_CONTROL notification captured so far uses this value (never
    # seen on a controller-issued ASSIGN command). Distinguishes the camera
    # reporting a value from the controller assigning one; exact official
    # spec meaning unconfirmed.
    CAMERA_REPORT = 0x02


@dataclass(frozen=True)
class CommandHeader:
    """Decoded/to-be-encoded BMD command packet header."""

    destination: int
    command_id: int
    category: int
    parameter: int
    data_type: DataType
    operation: Operation
    reserved: int = RESERVED_BYTE


def encode_packet(header: CommandHeader, payload: bytes = b"") -> bytes:
    """Encode a header and payload into a full BMD command packet."""
    length = LENGTH_FIELD_OFFSET + len(payload)
    if length > 0xFF:
        raise ValueError(f"Packet too large: length byte would be {length}, max 255")

    return bytes(
        [
            header.destination,
            length,
            header.command_id,
            header.reserved,
            header.category,
            header.parameter,
            int(header.data_type),
            int(header.operation),
        ]
    ) + bytes(payload)


def encode_assign(
    *,
    category: int,
    parameter: int,
    data_type: DataType,
    value: int,
    reserved: int = RESERVED_BYTE,
    command_id: int = 0x00,
    operation: Operation = Operation.ASSIGN,
) -> bytes:
    """Encode an ASSIGN-operation command packet for any category/parameter.

    Semantics-free: this function knows nothing about what the
    category/parameter pair means. Callers supply every value from a
    `CameraProfile` command block (or, for `tools/control/discover_command.py`,
    from an operator-driven candidate sweep) — never hardcoded.

    `operation` defaults to `Operation.ASSIGN`, matching every write this
    codebase has ever sent. Overridable for discovery-grade probing of the
    other write-capable operation, `OFFSET` (see
    `tools/control/send_settings_command.py --operation`, docs/settings.md
    §16) — no caller in this codebase passes anything but the default yet.
    """
    if data_type not in DATA_TYPE_STRUCT_FORMATS:
        raise ValueError(f"Unsupported data type for assign payload: {data_type!r}")

    header = CommandHeader(
        destination=DESTINATION_CAMERA,
        command_id=command_id,
        category=category,
        parameter=parameter,
        data_type=data_type,
        operation=operation,
        reserved=reserved,
    )
    width = DATA_TYPE_BYTE_WIDTHS[data_type]
    payload = value.to_bytes(width, byteorder="little", signed=value < 0)
    return encode_packet(header, payload)


def encode_assign_elements(
    *,
    category: int,
    parameter: int,
    data_type: DataType,
    values: Sequence[int],
    reserved: int = RESERVED_BYTE,
    command_id: int = 0x00,
    operation: Operation = Operation.ASSIGN,
) -> bytes:
    """Encode an ASSIGN command whose payload is several same-typed elements.

    The multi-element sibling of `encode_assign`, for parameters whose
    payload is a fixed sequence of values (e.g. a codec/variant id pair, or
    the five-int16 recording-format struct). Each element is packed at the
    data type's per-element width, little-endian, in the order given.

    Semantics-free like the rest of this module: element meaning, count, and
    every value come from a `CameraProfile` command block plus its lookup
    tables — never hardcoded here. `operation` defaults to `Operation.ASSIGN`
    and is overridable for the same discovery-grade reason as `encode_assign`.
    """
    if data_type not in DATA_TYPE_STRUCT_FORMATS:
        raise ValueError(f"Unsupported data type for assign payload: {data_type!r}")
    if not values:
        raise ValueError("encode_assign_elements needs at least one element value")

    header = CommandHeader(
        destination=DESTINATION_CAMERA,
        command_id=command_id,
        category=category,
        parameter=parameter,
        data_type=data_type,
        operation=operation,
        reserved=reserved,
    )
    width = DATA_TYPE_BYTE_WIDTHS[data_type]
    payload = b"".join(
        value.to_bytes(width, byteorder="little", signed=value < 0) for value in values
    )
    return encode_packet(header, payload)


def decode_packet(data: bytes) -> tuple[CommandHeader, bytes]:
    """Decode a full BMD command packet into its header and payload."""
    if len(data) < HEADER_LENGTH:
        raise ValueError(f"Packet too short: got {len(data)} bytes, need at least {HEADER_LENGTH}")

    length = data[1]
    expected_total = LENGTH_FIELD_OFFSET + length
    if len(data) != expected_total:
        raise ValueError(
            f"Length byte mismatch: header declares {length} remaining bytes "
            f"(total {expected_total}), but packet is {len(data)} bytes"
        )

    try:
        data_type = DataType(data[6])
    except ValueError as exc:
        raise ValueError(f"Unknown data type byte: 0x{data[6]:02X}") from exc

    try:
        operation = Operation(data[7])
    except ValueError as exc:
        raise ValueError(f"Unknown operation byte: 0x{data[7]:02X}") from exc

    header = CommandHeader(
        destination=data[0],
        command_id=data[2],
        reserved=data[3],
        category=data[4],
        parameter=data[5],
        data_type=data_type,
        operation=operation,
    )
    return header, bytes(data[HEADER_LENGTH:])


def header_matches(header: CommandHeader, *, category: int, parameter: int) -> bool:
    """Whether a decoded header's (category, parameter) matches the given pair.

    Shared by every `protocol/categories/*.py` module that needs to pick a
    specific notification out of the INCOMING_CONTROL stream before
    decoding its payload — semantics-free, like the rest of this module.
    """
    return header.category == category and header.parameter == parameter
