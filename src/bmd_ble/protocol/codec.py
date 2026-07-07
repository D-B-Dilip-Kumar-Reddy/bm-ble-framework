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
    Byte 7      Operation  (0x00 = assign, 0x01 = offset)
    Bytes 8+    Payload

This module only knows about the header. It has no knowledge of what a
particular category/parameter pair means — that lives in
protocol/categories/<category>.py once confirmed by a sniffer capture.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .types import DataType

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
