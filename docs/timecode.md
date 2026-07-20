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

**Frame rollover confirmed to track the configured recording frame rate.**
Captures on `POCKET_6K_PRO v8.6` at three different frame rates all show
`frames` rolling over at exactly the configured fps: max observed value 23
before wrapping at the default (~24fps) rate, 29 at 30fps, and 59 at 60fps
— i.e. `frames` counts `0` to `fps-1` and carries into `seconds` on wrap,
exactly like standard SMPTE-style timecode. This resolves what was
previously an open question (fixed-24 vs. fps-dependent): it's
fps-dependent, and the count matches whatever frame rate the camera is
actually recording at. Minutes/hours rollover still hasn't been exercised
in a capture (all captures so far are well under a minute).

**Why this doesn't change `duration_seconds` yet:** correctly carrying
`frames` into sub-second precision requires knowing the *current* recording
fps at the moment of each reading, and this framework doesn't track that —
FPS is part of the planned `settings.py` category (see CLAUDE.md's package
structure; not implemented). Once that exists, `duration_seconds` (or a new
variant) could compute `frames / fps` and add it in. Until then, `frames`
stays informational/display-only and duration remains hours/minutes/
seconds-only.

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
suppresses `ValueError`) since a callback must never raise. In real captures
TIMECODE notifications arrive frequently while recording — roughly every
60-120ms, not once a second — and stop arriving once recording stops.

`record_start()`/`record_stop()` snapshot into `last_start_timecode`/
`last_stop_timecode` **only after their echo has confirmed the state
change** — a failed/unconfirmed write does not snapshot anything, keeping
duration reporting tied to verified state transitions, not just "a write
happened."

**`record_start()` sets `last_start_timecode` to a canonical
`Timecode(0, 0, 0, 0)`, not whatever `_latest_timecode` currently holds.**
This is deliberate: real captures on both `POCKET_6K_G2 v7.9` and
`POCKET_6K_PRO v8.6` (and Blackmagic cameras generally, per direct hardware
observation) confirm TIMECODE resets to `00:00:00:00` the instant recording
starts. Snapshotting `_latest_timecode` at that point was a real bug — since
TIMECODE stops ticking while not recording, no new notification necessarily
arrives between the *previous* clip's stop and the *next* record_start's
echo confirmation, so the snapshot would silently carry over the previous
clip's stale end-of-clip reading as the new clip's "start," producing wrong
(and sometimes negative, hence `None`-duration) results. Using the known
reset behavior instead of racing to observe it sidesteps this entirely.

`record_stop()` still snapshots `_latest_timecode` as-is: TIMECODE ticks
continuously *during* the just-finished recording, so the last reading seen
before the stop echo confirms is a genuinely fresh in-recording value, not a
stale one — no equivalent bug here.

`last_clip_duration_seconds() -> float | None` returns the elapsed time
between the two snapshots, or `None` if `last_stop_timecode` is missing (no
TIMECODE notification had arrived during the recording — very unlikely given
the ~60-120ms cadence, but possible for an extremely short clip) or if
`stop` isn't after `start`. Since `last_start_timecode` is always the
canonical zero, this reduces to `last_stop_timecode`'s own hours/minutes/
seconds total.

`examples/record_start_stop.py` prints both raw timecode readings
(`HH:MM:SS:frames`) and the computed duration per cycle.

---

## What's next

`frames`-rollover-tracks-fps is now confirmed (three frame rates captured).
What's left: capture a longer recording (multiple minutes) to confirm
minutes/hours roll over correctly, and — once `settings.py`/FPS tracking
exists — extend `duration_seconds` (or add a variant) to fold `frames /
fps` into sub-second precision using the profile's/session's known current
frame rate.
