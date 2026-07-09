"""
bmd_ble/protocol/types.py
=========================
BMD command packet data type constants (packet header byte 6).

The codes follow the official *Blackmagic Camera Control Developer
Information* document and apply to every camera model and firmware version.
Model-specific values (codec IDs, variant IDs, ...) never belong here; they
live in payloads/models/<MODEL>_<FW>.json.

Provenance: the only data-type byte sniffer-verified over BLE so far is
``0x01`` (INT8) on the POCKET_6K_G2 v7.9 recording command and its echo. All
other codes are taken from the official spec and have not yet been observed
on real hardware — capture one before trusting a multi-byte decode. See
docs/protocol.md §3.
"""

from __future__ import annotations

from enum import IntEnum


class DataType(IntEnum):
    """BMD payload data type identifier (packet header byte 6).

    Code 0 is "void/boolean" in the official spec: a void parameter carries
    no payload (pure trigger), while a boolean parameter carries one byte per
    element (0 = false, non-zero = true). ``BOOL`` is therefore an alias of
    ``VOID`` — same wire code, ``DataType["BOOL"] is DataType.VOID``.
    """

    VOID = 0
    BOOL = 0  # alias — spec code 0 is "void/boolean"
    INT8 = 1
    INT16 = 2
    INT32 = 3
    INT64 = 4
    STRING = 5  # UTF-8
    FIXED16 = 128  # signed 5.11 fixed point: encoded = round(real * 2048)


# Fixed-width types only. Code 0 (VOID/BOOL) and STRING are omitted: a void
# payload is empty, a boolean payload is one byte per element, and a string
# is variable length — callers must handle those three explicitly.
DATA_TYPE_STRUCT_FORMATS: dict[DataType, str] = {
    DataType.INT8: "b",
    DataType.INT16: "h",
    DataType.INT32: "i",
    DataType.INT64: "q",
    DataType.FIXED16: "h",
}

# Byte width of one value of this type on the wire (little-endian). VOID is
# listed as 0 for the trigger reading of code 0; a boolean element under the
# same code is 1 byte (see the DataType docstring).
DATA_TYPE_BYTE_WIDTHS: dict[DataType, int] = {
    DataType.VOID: 0,
    DataType.INT8: 1,
    DataType.INT16: 2,
    DataType.INT32: 4,
    DataType.INT64: 8,
    DataType.FIXED16: 2,
}
