# Session and Verification

**Status:** implemented — `CameraSession` + `NotificationRouter` are live; recording verification is echo-only today.

## Overview

`session.py`'s `CameraSession` is the only surface user scripts touch
(CLAUDE.md design principle 5) — it composes `camera_controller.py` (BLE
transport) and `notification_router.py` (echo buffering) to provide
**verified** camera operations. This doc covers both new modules and the
current, deliberately minimal scope.

`examples/record_start_stop.py` is the first (and currently only) consumer.

---

## `NotificationRouter` (`src/bmd_ble/notification_router.py`)

Feature-agnostic: buffers decoded `INCOMING_CONTROL` notifications and lets
a caller await a fresh match by `(category, parameter)`. Has no knowledge of
what any category/parameter means — that lives in `protocol/categories/`.

```python
router.handle_incoming(characteristic, data)   # Bleak-style callback; never raises
router.arm(category, parameter)                # mark the current position as the baseline for this key
await router.wait_for(category, parameter, timeout)  # -> (CommandHeader, payload) | None
```

**Buffering starts at subscribe time, not at `wait_for` time.** `handle_incoming`
is registered as the `INCOMING_CONTROL` callback once, in
`CameraSession.__aenter__`, right after `connect()` — every notification
from then on is decoded and stored by `(category, parameter)`, regardless of
whether anything is currently awaiting a match. This is what CLAUDE.md means
by "subscribe and buffer before sending the command, not after — a router
that only starts listening after the write will race against the camera's
response."

**`arm` exists to avoid stale and duplicate matches.** Some commands (e.g.
recording start/stop) share a `(category, parameter)` key across opposite
states, and the camera has been observed retransmitting a single echo more
than once. This produces two distinct races, confirmed against real-hardware
logs where a `record_start`/`record_stop` pair back-to-back mismatched
(`echo confirmed recording=False, expected True` immediately followed by the
opposite):

1. A notification already buffered *before* `arm()` is called could
   otherwise be mistaken for a fresh one. Guarded with a per-key monotonic
   sequence number: `arm()` records the sequence number as of arm time, and
   `wait_for` only accepts a delivery whose sequence number is strictly
   greater.
2. A **duplicate retransmit of the previous command's echo** can arrive
   chronologically *after* the next command's `arm()` but *before* that
   next command's real echo — its sequence number looks fresh, but its
   bytes are identical to what was already consumed for the prior command.
   Since commands sharing a key always change the payload (recording
   start/stop toggle between two distinct values), `wait_for` also tracks
   the payload it last returned for a key and skips any delivery that
   repeats it, continuing to wait for a genuinely different one.

`CameraSession` always calls `arm()` immediately before
`write_outgoing_control`, so `wait_for` only returns on a delivery that is
both new-in-sequence and different in content from what the previous call
for that key consumed.

Undecodable notifications (e.g. `CAMERA_STATUS`'s raw single status byte,
which isn't a BMD command packet) are silently discarded — the router only
tracks packets it can actually attribute to a `(category, parameter)` key.

---

## `CameraSession` (`src/bmd_ble/session.py`)

```python
async with CameraSession("POCKET_6K_G2", "v7.9") as session:
    await session.record_start()   # raises BMDVerificationError unless confirmed
    await session.record_stop()
```

`__aenter__` loads the `CameraProfile`, scans, connects, and subscribes the
router — in that order, so buffering is active before any command could ever
be sent — then waits `connect_settle_s` (default `2.0`s) before returning.

**Why the settle wait exists:** real-hardware logs (both `POCKET_6K_G2 v7.9`
and `POCKET_6K_PRO v8.6`) showed a just-connected camera immediately floods
the link with an initial info dump (lens data, media/storage status,
battery, etc.). A command sent right after subscribing can queue behind that
backlog and take several seconds to echo — one `POCKET_6K_PRO` capture saw
the very first `record_start` after connecting echo back over 8 seconds
late, well past `echo_timeout_s`, while every later command in the same
session echoed back in well under a second. The failure was confined to the
first command of a fresh connection in every capture that showed it, on
both camera models, so the settle wait is unconditional — not gated by
camera model — rather than trying to detect "is the camera still busy" from
the notification stream. `connect_settle_s` is a first-pass, empirically
chosen value; it may need tuning if a longer initial backlog is observed on
other hardware.

`record_start`/`record_stop` build the command from the profile's
`commands.recording` block (`profile.require_command("recording", ("start",
"stop"))` → a `CommandSpec`, encoded via
`protocol.categories.recording.encode_record_start`/`encode_record_stop` —
never hardcoded), `arm` the router, write the command, then `wait_for` a
matching echo within `echo_timeout_s` (default `3.0`s — bumped up from an
initial `2.0`s after real-hardware logs showed occasional echo arrivals
taking close to that long, per CLAUDE.md's verification strategy). A timeout
or a mismatched confirmed state (echo says still-stopped after a start
command, or vice versa) raises
`BMDVerificationError` — the method only returns normally once the echo has
positively confirmed the requested state.

`profile.require_command("recording", ("start", "stop"))` raises a clear
`ValueError` naming the missing command block or value names *before*
attempting to build or send a command — used by
`tools/control/send_record_command.py` too, so both places fail the same way
on an unpopulated profile. See `docs/payload_profiles.md` for the profile
structure.

### Timecode tracking and clip duration

`__aenter__` also subscribes `TIMECODE`, storing the latest decoded reading
(`timecode.decode_timecode` — see `docs/timecode.md`) via a private callback.
A confirmed `record_start()` sets `last_start_timecode` to a canonical
`Timecode(0, 0, 0, 0)` rather than snapshotting the latest reading — real
hardware confirms TIMECODE resets to zero when recording starts, and
snapshotting `_latest_timecode` there was a real bug: TIMECODE stops ticking
while not recording, so the "latest" reading at that moment was often a
stale leftover from the *previous* clip's end, silently producing wrong
clip durations. A confirmed `record_stop()` still snapshots the latest
reading into `last_stop_timecode`, since TIMECODE ticks continuously during
the just-finished recording and so is genuinely fresh at that point. A
failed/unconfirmed write snapshots nothing either way.
`last_clip_duration_seconds() -> float | None` returns the elapsed time
between the two snapshots (hours/minutes/seconds precision only — see
`docs/timecode.md` for why), or `None` if `last_stop_timecode` is missing.

TIMECODE notifications are a full wrapped BMD-style packet, not a bare BCD
value — `decode_timecode` unwraps them via `protocol.codec.decode_packet`
before BCD-decoding the payload. See `docs/timecode.md` for the confirmed
wire format.

### Why `CAMERA_STATUS` isn't used as a secondary cross-check here

CLAUDE.md's verification strategy calls for an echo (primary) *and* a
`CAMERA_STATUS` read (secondary cross-check). For recording specifically,
this isn't implemented, because it isn't currently possible: the only
documented `CAMERA_STATUS` bits (`constants.py`) are Camera Power On,
Connected, Paired, Versions Verified, Initial Payload Received, and Camera
Ready — **none of them encode recording state**. There is no known
`CAMERA_STATUS` bit to cross-check "is recording" against yet. Rather than
faking a cross-check against unrelated bits, `record_start`/`record_stop`
verify via the `INCOMING_CONTROL` echo only. If a future sniffer capture
discovers a recording-related `CAMERA_STATUS` bit (or this camera's status
byte gets fully decoded), the secondary check can be added here.

---

## What's deliberately out of scope

- **Storage preconditions** (CLAUDE.md design principle 10) — checking
  `CameraState.storage` before recording requires notification-driven
  storage state tracking, which doesn't exist yet (no `CameraState`, no
  `StorageState`, no storage characteristic parsing). Not implemented.
- **GAP/device metadata passthrough** — `camera_controller.py` already
  exposes `read_gap_identity_metadata`/`read_device_information_metadata`;
  `CameraSession` doesn't wrap them yet.
- **Reconnect wiring beyond `camera_controller.connect()`'s own behavior** —
  no additional session-level retry/backoff logic.
- **Any category other than recording** — `CameraSession` only has
  `record_start`/`record_stop` today.

---

## Testing

`tests/unit/test_notification_router.py` covers `arm`/`wait_for` timing
(match delivered before vs. after `wait_for` is called, timeout returns
`None`, `arm` rejects a match already buffered before it was called) and
that undecodable notifications are ignored, plus a regression test for the
duplicate-retransmit race described above (a stale retransmit of the
previous command's echo, delivered after `arm()`, must not satisfy
`wait_for` — only a payload distinct from the last consumed one does).
`tests/unit/test_session.py` mocks `BMDCameraController` and
`NotificationRouter` to cover: success on a matching echo, `BMDVerificationError`
on timeout, `BMDVerificationError` on a mismatched confirmed state, missing
profile command/values raising before any write is attempted, TIMECODE
snapshot capture (including that a failed verification snapshots nothing,
and that no TIMECODE reading yet yields `None`), and
`last_clip_duration_seconds()`. No real BLE in either test file.
`tests/unit/test_timecode.py` covers `decode_timecode`/`duration_seconds`
directly — see `docs/timecode.md`.
