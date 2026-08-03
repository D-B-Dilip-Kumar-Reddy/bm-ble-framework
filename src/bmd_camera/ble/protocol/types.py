"""
bmd_camera/ble/protocol/types.py
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
docs/ble/protocol.md §3.

One exception to "official spec coding": ``INT16_ARRAY`` (``0x82``) is not
in the official document at all. It was reported on the POCKET_6K_G2 v7.9
recording-format write packet (category 0x01, parameter 0x09 — five int16
elements) by an external reverse-engineering effort and is carried here as a
CANDIDATE wire value so those packets can be encoded and decoded — the same
precedent as ``Operation.CAMERA_REPORT`` in codec.py. It has not yet been
re-verified by this repo's own capture tooling. See docs/ble/settings.md.
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
    # Not in the official spec — CANDIDATE wire value reported on the
    # POCKET_6K_G2 v7.9 recording-format packet (five little-endian int16
    # elements). Hypothesis: 0x80 | INT16 = "array of int16", but FIXED16
    # already occupies 0x80 so the flag reading stays unconfirmed. See the
    # module docstring and docs/ble/settings.md.
    INT16_ARRAY = 130


# Fixed-width types only. Code 0 (VOID/BOOL) and STRING are omitted: a void
# payload is empty, a boolean payload is one byte per element, and a string
# is variable length — callers must handle those three explicitly. For
# INT16_ARRAY the format/width describe ONE element; the element count is a
# per-parameter fact supplied by the caller (see codec.encode_assign_elements).
DATA_TYPE_STRUCT_FORMATS: dict[DataType, str] = {
    DataType.INT8: "b",
    DataType.INT16: "h",
    DataType.INT32: "i",
    DataType.INT64: "q",
    DataType.FIXED16: "h",
    DataType.INT16_ARRAY: "h",
}

# Byte width of one value of this type on the wire (little-endian). VOID is
# listed as 0 for the trigger reading of code 0; a boolean element under the
# same code is 1 byte (see the DataType docstring). Multi-element types list
# the per-element width.
DATA_TYPE_BYTE_WIDTHS: dict[DataType, int] = {
    DataType.VOID: 0,
    DataType.INT8: 1,
    DataType.INT16: 2,
    DataType.INT32: 4,
    DataType.INT64: 8,
    DataType.FIXED16: 2,
    DataType.INT16_ARRAY: 2,
}
