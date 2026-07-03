"""
bmd_ble/protocol/types.py
=========================
BMD command packet data type constants (packet header byte 6).

These values are fixed by the BMD BLE Camera Control protocol and apply to
every camera model and firmware version — see the "Data types" table in
CLAUDE.md. Model-specific values (codec IDs, variant IDs, ...) never belong
here; they live in payloads/models/<MODEL>_<FW>.json.
"""

from __future__ import annotations

from enum import IntEnum


class DataType(IntEnum):
    """BMD payload data type identifier (packet header byte 6)."""

    VOID = 0
    BOOL = 1
    INT8 = 2
    INT16 = 3
    INT32 = 4
    INT64 = 5
    STRING = 6
    FIXED16 = 7


# Fixed-width types only. VOID carries no payload and STRING is variable
# length, so both are omitted — callers must handle those two separately.
DATA_TYPE_STRUCT_FORMATS: dict[DataType, str] = {
    DataType.BOOL: "?",
    DataType.INT8: "b",
    DataType.INT16: "h",
    DataType.INT32: "i",
    DataType.INT64: "q",
    DataType.FIXED16: "h",
}

# Byte width of one value of this type on the wire (little-endian).
DATA_TYPE_BYTE_WIDTHS: dict[DataType, int] = {
    DataType.VOID: 0,
    DataType.BOOL: 1,
    DataType.INT8: 1,
    DataType.INT16: 2,
    DataType.INT32: 4,
    DataType.INT64: 8,
    DataType.FIXED16: 2,
}
