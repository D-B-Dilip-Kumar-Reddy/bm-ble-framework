# Timecode and Clip Duration

## Overview

`src/bmd_ble/timecode.py` decodes the `TIMECODE` characteristic and computes
elapsed duration between two readings. It lives alongside `session.py` and
`camera_controller.py`, not under `protocol/categories/` — TIMECODE isn't a
general SDI command sent/echoed through `OUTGOING_CONTROL`/`INCOMING_CONTROL`,
it's a single fixed reading pushed by a distinct BLE characteristic.
`CameraSession` uses it to snapshot the camera's timecode around
`record_start()`/`record_stop()`, so a script can report how long a clip
actually was.

**Status: wire format confirmed by real capture on two camera models.**
Real `TIMECODE` notifications captured on both `POCKET_6K_G2 v7.9` and
`POCKET_6K_PRO v8.6` (via `examples/record_start_stop.py`, run against real
hardware) disproved the original assumption that TIMECODE is a bare 32-bit
BCD value directly on the characteristic. `constants.py`'s `HH:MM:SS:mm`
comment describes the human-readable field order, not the wire encoding.

The real bytes are a full BMD-style packet — the same header shape as
`OUTGOING_CONTROL`/`INCOMING_CONTROL` (see `protocol/codec.py`) — wrapping a
4-byte BCD payload:

```
FF 08 00 FF 09 04 03 00 23 00 00 00
```

Decoded via `protocol.codec.decode_packet`: `destination=0xFF`,
`command_id=0x00`, `reserved=0xFF`, `category=0x09`, `parameter=0x04`,
`data_type=0x03` (`INT32`), `operation=0x00` (`ASSIGN`), 4-byte payload
`23 00 00 00`. This exact header signature appears on every single TIMECODE
notification captured across multiple sessions on both cameras.

`timecode.py` therefore calls `decode_packet` to unwrap the header, then
BCD-decodes the 4-byte payload in **`[frames, seconds, minutes, hours]`**
order — least-significant field first, the *reverse* of the `HH:MM:SS:mm`
display order. This was determined by tracing a long contiguous run of real
readings: the first payload byte (BCD-decoded) cycles `0-23` and wraps, and
the second payload byte increments by exactly 1 each time the first wraps —
consistent with a 24-count frame counter carrying into seconds.

**What's still unconfirmed:** whether the frame field's rollover point (24)
is fixed or depends on the camera's configured frame rate — only a 24-ish
fps recording has been observed carrying so far, and minutes/hours haven't
been exercised in a capture. Treat `frames` as informational/display-only
until a capture spans a different fps or a longer duration.

---

## `Timecode` / `decode_timecode` (`src/bmd_ble/timecode.py`)

```python
TIMECODE_CATEGORY = 0x09
TIMECODE_PARAMETER = 0x04

@dataclass(frozen=True)
class Timecode:
    hours: int
    minutes: int
    seconds: int
    frames: int  # rollover semantics beyond 24fps-shaped captures unconfirmed

def decode_timecode(data: bytes) -> Timecode
```

`decode_timecode` unwraps `data` via `protocol.codec.decode_packet`, then
raises `ValueError` if the header's `(category, parameter)` isn't
`(0x09, 0x04)`, if `data_type` isn't `INT32`, or if the payload isn't 4
bytes. Otherwise it BCD-decodes the payload as `[frames, seconds, minutes,
hours]` into `Timecode(hours, minutes, seconds, frames)`.

`TIMECODE_CATEGORY`/`TIMECODE_PARAMETER` are treated as fixed constants
here (like `protocol/codec.py`'s `Operation` values), not per-model profile
data — they were sniffer-verified identical on both cameras captured so
far. If a future camera model is sniffed with a different TIMECODE header,
revisit this.

## `duration_seconds`

```python
def duration_seconds(start: Timecode, stop: Timecode) -> float
```

Computes elapsed seconds from **hours/minutes/seconds only** — `frames` is
excluded deliberately. Correctly carrying it into whole seconds requires
confirming its rollover point holds across frame rates, which isn't
confirmed yet. Using it without that knowledge would silently produce a
wrong answer some fraction of the time — worse than a duration that's
simply less precise. Raises `ValueError` if `stop` isn't strictly after
`start` (no midnight/24h-rollover handling — out of scope until real data
shows a recording spans it).

---

## How `CameraSession` uses it

`CameraSession.__aenter__` subscribes `TIMECODE` (alongside the existing
`INCOMING_CONTROL` subscribe) with a callback that decodes and stores the
latest reading; decode failures are silently ignored there (`_handle_timecode`
suppresses `ValueError`) since a callback must never raise.
`record_start()`/`record_stop()` each snapshot that latest reading into
`last_start_timecode`/`last_stop_timecode` **only after their echo has
confirmed the state change** — a failed/unconfirmed write does not snapshot
anything, keeping duration reporting tied to verified state transitions, not
just "a write happened."

`last_clip_duration_seconds() -> float | None` returns the elapsed time
between the two snapshots, or `None` if either is missing (no TIMECODE
notification had arrived yet at that moment — TIMECODE ticks roughly once a
second, so a very fast start-then-stop could plausibly miss one) or if
`stop` isn't after `start`.

`examples/record_start_stop.py` prints both raw timecode readings
(`HH:MM:SS:frames`) and the computed duration per cycle.

---

## What's next

Capture a longer recording (multiple minutes, and at a frame rate other than
the ~24fps-shaped one observed so far) and check whether the `frames` field's
rollover point changes with fps, and whether minutes/hours roll over
correctly. Once confirmed to be reliable, `duration_seconds` can be extended
to carry `frames` into sub-second precision.
