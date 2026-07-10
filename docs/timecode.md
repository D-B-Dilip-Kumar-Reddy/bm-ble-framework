# Timecode and Clip Duration

## Overview

`src/bmd_ble/timecode.py` decodes the `TIMECODE` characteristic and computes
elapsed duration between two readings. It lives alongside `session.py` and
`camera_controller.py`, not under `protocol/` — TIMECODE is not a BMD
command packet, it's a distinct BLE characteristic with its own 32-bit BCD
encoding (`constants.py`: `HH:MM:SS:mm`, e.g. `09:12:53:10 = 0x09125310`).
`CameraSession` uses it to snapshot the camera's timecode around
`record_start()`/`record_stop()`, so a script can report how long a clip
actually was.

**Status: mechanically decoded, semantically unconfirmed.** The 4-byte BCD
layout (one field per byte: hours, minutes, seconds, a 4th field) is a fixed
encoding and safe to decode as documented — this part isn't camera-specific
and doesn't need a sniffer capture to trust, the same way the packet header's
*byte positions* turned out to be trustworthy even after the *length-field
counting* assumption was wrong (see `docs/packet_structure_and_constants.md`).
What's **not** confirmed: what the 4th field actually represents (video
frame number? centiseconds? something else?) and what value it rolls over
at. No real TIMECODE bytes have been captured and inspected yet.

---

## `Timecode` / `decode_timecode` (`src/bmd_ble/timecode.py`)

```python
@dataclass(frozen=True)
class Timecode:
    hours: int
    minutes: int
    seconds: int
    subfield: int  # meaning NOT yet confirmed

def decode_timecode(data: bytes) -> Timecode
```

Each byte is one BCD-encoded two-digit field, in the order
`[hours, minutes, seconds, subfield]` — matching the documented example
(`0x09125310` → `09:12:53:10`). Raises `ValueError` if `data` isn't exactly
4 bytes.

## `duration_seconds`

```python
def duration_seconds(start: Timecode, stop: Timecode) -> float
```

Computes elapsed seconds from **hours/minutes/seconds only** — `subfield` is
excluded deliberately. Correctly carrying a sub-second field into whole
seconds requires knowing its rollover point (e.g. is it out of 100? out of
the camera's configured fps?), which isn't confirmed. Using it without that
knowledge would silently produce a wrong answer some fraction of the time —
worse than a duration that's simply less precise. Raises `ValueError` if
`stop` isn't strictly after `start` (no midnight/24h-rollover handling —
out of scope until real data shows a recording spans it).

---

## How `CameraSession` uses it

`CameraSession.__aenter__` subscribes `TIMECODE` (alongside the existing
`INCOMING_CONTROL` subscribe) with a callback that decodes and stores the
latest reading. `record_start()`/`record_stop()` each snapshot that latest
reading into `last_start_timecode`/`last_stop_timecode` **only after their
echo has confirmed the state change** — a failed/unconfirmed write does not
snapshot anything, keeping duration reporting tied to verified state
transitions, not just "a write happened."

`last_clip_duration_seconds() -> float | None` returns the elapsed time
between the two snapshots, or `None` if either is missing (no TIMECODE
notification had arrived yet at that moment — TIMECODE ticks roughly once a
second, so a very fast start-then-stop could plausibly miss one) or if
`stop` isn't after `start`.

`examples/record_start_stop.py` prints both raw timecode readings and the
computed duration per cycle.

---

## What's next

Run `examples/record_start_stop.py` (or any script using `CameraSession`)
against real hardware and inspect the printed `HH:MM:SS:subfield` readings:

- If `subfield` matches the camera's configured frame rate range (e.g. maxes
  out at 23/24/29 for common fps values), it's very likely frame number —
  `duration_seconds` can then be extended to carry it correctly once the fps
  is known (from the profile, once frame-rate settings are implemented) or
  observed directly (does it roll over at a fixed value regardless of
  camera settings, or does it vary?).
- If it looks like it counts 0–99, it's more likely centiseconds/hundredths,
  which would need a different (much simpler, fixed) rollover.

Until then, treat `subfield` as informational/display-only.
