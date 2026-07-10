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
router.arm(category, parameter)                # clear any stale match for this key
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

**`arm` exists to avoid stale matches.** Without it, a leftover match stored
from an earlier, unrelated command with the same `(category, parameter)`
could satisfy a `wait_for` call instantly, without the camera having actually
responded to *this* write. `CameraSession` always calls `arm()` immediately
before `write_outgoing_control`, clearing that key's stored match and Event
so `wait_for` only returns on a genuinely fresh notification.

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
be sent. `record_start`/`record_stop` build the command from the profile's
`commands.recording` block (`profile.require_command("recording", ("start",
"stop"))` → a `CommandSpec`, encoded via
`protocol.categories.recording.encode_record_start`/`encode_record_stop` —
never hardcoded), `arm` the router, write the command, then `wait_for` a
matching echo within `echo_timeout_s` (default `2.0`, per CLAUDE.md's
verification strategy). A timeout or a mismatched confirmed state (echo says
still-stopped after a start command, or vice versa) raises
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
`record_start()`/`record_stop()` each snapshot that latest reading into
`last_start_timecode`/`last_stop_timecode` immediately after their echo
confirms the state change — a failed/unconfirmed write snapshots nothing.
`last_clip_duration_seconds() -> float | None` returns the elapsed time
between the two snapshots (hours/minutes/seconds precision only — see
`docs/timecode.md` for why), or `None` if either snapshot is missing.

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
`None`, `arm` clears a stale match) and that undecodable notifications are
ignored. `tests/unit/test_session.py` mocks `BMDCameraController` and
`NotificationRouter` to cover: success on a matching echo, `BMDVerificationError`
on timeout, `BMDVerificationError` on a mismatched confirmed state, missing
profile command/values raising before any write is attempted, TIMECODE
snapshot capture (including that a failed verification snapshots nothing,
and that no TIMECODE reading yet yields `None`), and
`last_clip_duration_seconds()`. No real BLE in either test file.
`tests/unit/test_timecode.py` covers `decode_timecode`/`duration_seconds`
directly — see `docs/timecode.md`.
