# Packet Structure and Constants

## Overview

`protocol/codec.py` and `protocol/types.py` implement the BMD command packet
layer: encoding and decoding the fixed 8-byte header defined by the BMD BLE
Camera Control protocol, and the data type enumeration carried in that header.
Neither module has any BLE knowledge — they operate purely on `bytes`.

---

## Packet Structure

```
Byte 0      Destination device (0x00 = camera)
Byte 1      Length of remaining data (bytes 2 onwards)
Byte 2      Command ID / type
Byte 3      Reserved
Byte 4      Category
Byte 5      Parameter
Byte 6      Data type
Byte 7      Operation  (0x00 = assign, 0x01 = offset)
Bytes 8+    Payload
```

`HEADER_LENGTH` (`protocol/codec.py`) is 8 — bytes 0 through 7. The length
byte (byte 1) covers bytes 2 onwards only: 6 fixed header bytes
(`HEADER_REMAINDER_LENGTH`) plus the payload length.

### `CommandHeader`

```python
@dataclass(frozen=True)
class CommandHeader:
    destination: int
    command_id: int
    category: int
    parameter: int
    data_type: DataType
    operation: Operation
    reserved: int = RESERVED_BYTE
```

`encode_packet(header, payload=b"")` builds the 8-byte header followed by the
payload, computing the length byte automatically. `decode_packet(data)`
parses a full packet back into `(CommandHeader, payload_bytes)`, raising
`ValueError` if the buffer is shorter than `HEADER_LENGTH`, the length byte
doesn't match the actual buffer size, or the data type / operation byte is
outside the known enum range.

The reserved byte is not validated against `RESERVED_BYTE` on decode — it is
surfaced as-is on `CommandHeader.reserved` in case real hardware ever sends a
non-zero value, which would otherwise be silently discarded.

### `Operation`

```python
class Operation(IntEnum):
    ASSIGN = 0x00
    OFFSET = 0x01
```

Fixed by the protocol spec (packet header byte 7). Lives in `codec.py`
because it is a header structural field, not a payload data type.

---

## Data Types (`protocol/types.py`)

```python
class DataType(IntEnum):
    VOID = 0
    BOOL = 1
    INT8 = 2
    INT16 = 3
    INT32 = 4
    INT64 = 5
    STRING = 6
    FIXED16 = 7
```

`DATA_TYPE_BYTE_WIDTHS` gives the on-the-wire byte width for each type
(`VOID` is 0, `STRING` is intentionally omitted since it's variable-length).
`DATA_TYPE_STRUCT_FORMATS` gives the little-endian `struct` format code for
every fixed-width type, for packing/unpacking payload bytes.

`FIXED16` is decoded as a raw signed 16-bit integer only — no scale-factor
conversion to a float is implemented. The BMD spec's fixed-point
interpretation for `FIXED16` hasn't been confirmed against a sniffer capture
in this repo yet, so it is intentionally left as a TODO rather than assumed.

---

## Where Constants Live

| Kind of constant | Example | Lives in |
|---|---|---|
| BLE service/characteristic UUIDs | `BMD_SERVICE_UUID` | `constants.py` |
| Characteristic display names | `CHARACTERISTIC_NAMES` | `constants.py` |
| BLE timing (scan/connect/reconnect) | `BLE_SCAN_TIMEOUT_S` | `constants.py` |
| Packet header structure (destination byte, reserved byte, operation codes) | `DESTINATION_CAMERA`, `Operation` | `protocol/codec.py` |
| Payload data types | `DataType` | `protocol/types.py` |
| Category ID + parameter IDs for one command family | `CATEGORY_ID` in a category file | `protocol/categories/<category>.py` — added only after a sniffer capture confirms them on real hardware |
| Codec IDs, quality/resolution/FPS encodings, capability flags, storage UUIDs | `codec_ids`, `fps_encodings`, `supports_raw` | `payloads/models/<MODEL>_<FW>.json`, via `CameraProfile` |

**Rule of thumb:** if a value is fixed by the Bluetooth or BMD spec and
applies to every camera regardless of model/firmware, it's a constant in
`constants.py` (BLE/transport) or `protocol/` (packet structure). If it can
differ by model or firmware, it's data in a profile JSON, read through
`CameraProfile`. Category/parameter byte values are spec-fixed but are only
added to `protocol/categories/*.py` once confirmed by an actual sniffer
capture — never invented ahead of hardware verification.

### Known gap

`CAMERA_STATUS` bitfield flags (Camera Power On, Connected, Paired, Versions
Verified, Initial Payload Received, Camera Ready) are documented only as a
comment in `constants.py` today. They are spec-fixed and belong there as real
named constants, but adding them is unrelated to the packet-structure work
this doc covers and is left for a future change.
