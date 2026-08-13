# Packet Structure and Constants

**Status:** implemented — `protocol/codec.py` + `protocol/types.py`; structure sniffer-verified on `POCKET_6K_G2 v7.9`.

## Overview

`protocol/codec.py` and `protocol/types.py` implement the BMD command packet
layer: encoding and decoding the fixed 8-byte header defined by the BMD BLE
Camera Control protocol, and the data type enumeration carried in that header.
Neither module has any BLE knowledge — they operate purely on `bytes`.

---

## Packet Structure

```
Byte 0      Fixed prefix byte (0xFF)
Byte 1      Length field — counts only bytes 4 onwards
Byte 2      Command ID / type
Byte 3      Reserved
Byte 4      Category
Byte 5      Parameter
Byte 6      Data type
Byte 7      Operation  (0x00 = assign, 0x01 = offset, 0x02 = camera report)
Bytes 8+    Payload
```

`HEADER_LENGTH` (`protocol/codec.py`) is 8 — bytes 0 through 7, unchanged.
The length byte (byte 1) covers bytes 4 onwards only: category, parameter,
data type, operation (`LENGTH_FIELD_OFFSET = 4` bytes not counted: prefix,
length byte itself, command_id, reserved), plus the payload length.

**Corrected after a real sniffer capture.** This module originally assumed
byte 0 was a `0x00` "destination device" byte and that the length field
counted everything from byte 2 onwards (6 fixed bytes + payload) — lifted
from the generic BMD spec and never verified against real BLE hardware. A
capture on `POCKET_6K_G2 v7.9` (via `tools/sniffers/sniffer_recording.py`)
showed a 100% decode failure rate: every single captured packet's actual
size was exactly `declared_length + 4`, not `declared_length + 2`, and byte 0
was always `0xFF` in both directions. Cross-checked directly against a known,
reverse-engineered record start command (`FF 05 00 01 0A 01 01 00 02`), the
corrected formula reproduces it byte-for-byte. See `docs/ble/recording.md` and
CLAUDE.md's "Packet structure" section.

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

Every confirmed command family until 2026-07-27 needed one *specific*
reserved byte (recording's `0x01` vs the `0x00` default elsewhere — see
`docs/ble/protocol.md` §6). `commands.photo` broke that pattern: both `0x00`
and `0x01` independently triggered a real photo capture (SD-card-verified)
on `POCKET_6K_G2 v7.9` (`docs/ble/photo_capture.md` §7), and the same
indifference was then independently reproduced on `POCKET_6K_PRO v8.6`
(§9) — not a one-camera quirk. The byte is recorded as indifferent for
this command rather than as a value the camera checks. `0x00` is stored in
each profile as the canonical value (this codebase's own default) purely
for convention, not because it was distinguished from `0x01` on the wire.

`encode_assign(*, category, parameter, data_type, value, reserved=RESERVED_BYTE,
command_id=0x00, operation=Operation.ASSIGN)` builds a complete command packet
(header + little-endian payload) for any category/parameter — the codec now
owns generic assign-packet building, still with zero category *semantics*:
every value is caller-supplied, from a `CameraProfile` command block or from
`tools/control/discover_command.py`'s candidate sweep.
`protocol/categories/recording.py`'s encoder delegates to it. Despite the
function's name, `operation` (added 2026-07-24) defaults to but is not
locked to `Operation.ASSIGN` — every caller in this codebase still passes
only the default, but `tools/control/send_settings_command.py --operation`
uses the override to probe `Operation.OFFSET`, never tried for any settings
family (see `docs/ble/settings.md` §16).

`encode_assign_elements(*, category, parameter, data_type, values,
reserved=RESERVED_BYTE, command_id=0x00, operation=Operation.ASSIGN)` is its
multi-element sibling for parameters whose payload is a fixed sequence of
same-typed values (a codec/variant id pair, the five-int16 recording-format
struct, ...): each element is packed at the data type's per-element width,
little-endian, in the order given. Same zero-semantics stance and the same
overridable `operation`; consumed by `protocol/categories/settings.py` (see
`docs/ble/settings.md`).

`encode_assign_void(*, category, parameter, reserved=RESERVED_BYTE,
command_id=0x00, operation=Operation.ASSIGN)` (added 2026-07-27) is the
payloadless sibling for the spec's void trigger parameters (one-shot AF
0.1, still capture 10.3, ...): header only, length byte exactly
`LENGTH_FIELD_OFFSET` — the same shape as the sniffer-verified void camera
reports in the 2026-07-27 photo-capture baselines. It is deliberately a
separate function rather than a `value=None` mode of `encode_assign`: the
`DATA_TYPE_STRUCT_FORMATS` gate in `encode_assign` is what stops a caller
from silently encoding a zero-byte payload for a fixed-width type, and
keeping VOID out of that table preserves the guarantee. Consumed by
`tools/common/discovery.py`'s VOID candidate sweeps (see
`docs/ble/command_discovery.md`).

### `Operation`

```python
class Operation(IntEnum):
    ASSIGN = 0x00
    OFFSET = 0x01
    CAMERA_REPORT = 0x02
```

Fixed by the protocol spec (packet header byte 7). Lives in `codec.py`
because it is a header structural field, not a payload data type.

`CAMERA_REPORT` was added after a real sniffer capture on `POCKET_6K_G2 v7.9`
showed every camera-originated `INCOMING_CONTROL` notification using this
value — never seen on a controller-issued `ASSIGN` command. Its exact
official spec meaning is unconfirmed; the name reflects what's been directly
observed (see `docs/ble/recording.md`, "The echo has been observed").

`OFFSET` has never been sent by this codebase — every write, on every
category/parameter, on both cameras, has used `ASSIGN`. `encode_assign`/
`encode_assign_elements` gained an overridable `operation` parameter
(2026-07-24, default `Operation.ASSIGN`, every existing caller unaffected)
specifically to make `OFFSET` testable as a discovery-grade probe — see
`tools/control/send_settings_command.py --operation` and `docs/ble/settings.md`
§16 for the reverse-engineering case that motivated it (a settings retarget
that never confirms under `ASSIGN`, with every value-level hypothesis
already exhausted). `OFFSET`'s semantics for any category/parameter remain
unconfirmed.

---

## Data Types (`protocol/types.py`)

The enum follows the official spec's coding (see `docs/ble/protocol.md` §3 for
the full table, provenance, and the 2026-07-09 remap history):

```python
class DataType(IntEnum):
    VOID = 0
    BOOL = 0  # alias — spec code 0 is "void/boolean"
    INT8 = 1
    INT16 = 2
    INT32 = 3
    INT64 = 4
    STRING = 5
    FIXED16 = 128
    INT16_ARRAY = 130  # 0x82 — NOT official coding; CANDIDATE, see below
```

`INT16_ARRAY` (`0x82`) is the one member outside the official coding: a
CANDIDATE wire byte reported on `POCKET_6K_G2 v7.9`'s recording-format
packet (five little-endian int16 elements — see `docs/ble/settings.md` §3),
carried on the same "observed on the wire, absent from the public spec"
precedent as `Operation.CAMERA_REPORT`. Its width/format entries describe
one *element*; the element count is per-parameter and supplied by the
caller.

`DATA_TYPE_BYTE_WIDTHS` gives the on-the-wire byte width for each type
(`VOID` is 0 for the trigger reading of code 0; `STRING` is intentionally
omitted since it's variable-length). `DATA_TYPE_STRUCT_FORMATS` gives the
little-endian `struct` format code for every fixed-width type, for
packing/unpacking payload bytes; code 0 and `STRING` are excluded — callers
handle those explicitly.

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
| Packet header structure (prefix byte, reserved byte, operation codes, length-field offset) | `DESTINATION_CAMERA`, `LENGTH_FIELD_OFFSET`, `Operation` | `protocol/codec.py` |
| Payload data types | `DataType` | `protocol/types.py` |
| Category ID + parameter IDs for one command family | `CATEGORY_ID` in a category file | `protocol/categories/<category>.py` — added only after a sniffer capture confirms them on real hardware |
| Codec IDs, quality/resolution/FPS encodings, capability flags, storage UUIDs | `codecs`, `resolutions`, `fps_modes`, `supports_raw` | `payloads/models/<MODEL>_<FW>.json`, via `CameraProfile` |

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
