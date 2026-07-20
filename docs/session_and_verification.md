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
2. A **duplicate retransmit** of an echo already visible when `arm()` is
   called can arrive chronologically *after* `arm()` but *before* the real
   new echo — its sequence number looks fresh, but its bytes are identical
   to whatever the stream already showed at arm time. `arm()` snapshots
   that payload (whatever it is, whether or not anything ever consumed it —
   see below), and `wait_for` rejects any delivery matching it. Since
   commands sharing a key always change the payload (recording start/stop
   toggle between two distinct values), a genuine new echo can never equal
   what the stream showed *before* the new command was sent, so this never
   rejects a real one.

   This is deliberately keyed off "what `arm()` saw", not "what `wait_for`
   last *returned*" — an earlier version tracked the latter and had a real
   bug: if a command's own `wait_for` timed out (nothing consumed), the
   stored "last returned" value went stale, and the *next* command's
   genuinely fresh echo could be wrongly rejected if it happened to share a
   value with an older, unrelated consumption. Real-hardware logs where a
   camera auto-stopped a recording without any command from this repo
   surfaced this exact bug — see "Camera-initiated stop detection" below —
   and `tests/unit/test_notification_router.py`'s
   `test_fresh_echo_accepted_even_if_it_matches_an_older_unconsumed_value`
   is a regression test for it.

`CameraSession` always calls `arm()` immediately before
`write_outgoing_control`, so `wait_for` only returns on a delivery that is
both new-in-sequence and different in content from what `arm()` had already
seen.

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
be sent — then waits `connect_settle_s` (default `6.0`s — bumped up from an
initial `2.0`s once real captures showed the first-command echo delay could
run past 8 seconds) before returning.

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
the notification stream. `connect_settle_s` is empirically chosen; it may
need further tuning if a longer initial backlog is observed on other
hardware.

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

### Settings writes (`set_codec_quality` / `set_video_format` / `set_recording_format`)

The three settings methods follow the same profile-driven
arm-write-await-echo pattern as recording, against the packet families in
`docs/settings.md` (which also tabulates exactly which decoded payload
elements each method compares, and which it deliberately doesn't). All
three are now confirmed VERIFIED on real hardware through `CameraSession`
itself, not just a raw send tool: `set_video_format` via 2/2 round trips
(`docs/settings.md` §8), `set_codec_quality` and `set_recording_format`
via one genuine cycle each, surfaced by `set_camera_format`'s proxy path
(`docs/settings.md` §10). Two deviations from the recording flow:

- **`set_video_format` arms three echo channels.** Its own `0x01/0x00`
  (never observed to fire in practice), the recording-format coordinates
  `0x01/0x09` ("mode-notify"), and — added after a real bug, below — the
  `codec_quality` coordinates `0x0A/0x00`. A `_wait_first_echo` helper runs
  one `NotificationRouter.wait_for` task per armed key concurrently and
  takes the first fresh delivery from any, cancelling the rest; each
  channel's payload is decoded per its own family (the `0x0A/0x00` branch
  only checks the reported `codec_id`, since this method takes no
  `variant` argument to compare against). All keys are armed *before* the
  write, per the router's usual staleness contract.

  **Why three, not two — a real bug found on real hardware
  (`docs/settings.md` §10):** the mode-notify payload encodes
  fps/width/height only, never codec. A `set_video_format` call that
  changes *only* the codec family (same resolution and fps — e.g. 4K
  DCI/ProRes → 4K DCI/BRAW) produces a mode-notify report byte-identical
  to what `NotificationRouter` already saw before the write, so its
  stale-duplicate filter (see `NotificationRouter`'s own docstring below —
  it exists to protect other families from genuine retransmit duplicates,
  and is not being weakened) discards the fresh report, and the call
  spuriously raised `BMDVerificationError` even though the camera's
  `0x0A/0x00` report (which reliably follows *every* `video_format` write,
  confirmed since `docs/settings.md` §8) carried real confirmation the
  whole time. Watching that channel too closes the gap: a codec-only
  switch always changes what it reports, even when it can't change
  mode-notify's content.
- **Precondition failures raise before any write.** `BMDUnsupportedError`
  (its first use in the repo — CLAUDE.md design principle 7) when the
  profile says the camera doesn't offer the requested codec at that
  resolution; `ValueError` pointing at the capture workflow when the
  combination is supported but its `dimension_enum` hasn't been
  reverse-engineered yet.

`set_codec_quality` has a confirmed false-positive mode independent of the
bug above: real hardware showed the camera's `0x0A/0x00` report only fires
on an *actual applied change* — a call requesting the (codec, variant) the
camera is already at (easy to hit right after a `set_video_format` switch,
since each codec family remembers its own last-set quality independently)
produces no report and a `BMDVerificationError: no echo received`
indistinguishable from a genuine failure. Mirrors `record_stop()`'s
documented no-echo-on-redundant-command behavior (`docs/recording.md`) —
but unlike `record_stop()`, `set_codec_quality` has no `is_recording`-style
guard to skip a redundant write, since `CameraSession` doesn't track
current codec/quality state (no `CameraState` yet, design principle 4);
the error message names this possibility instead, and `set_camera_format`
(below) doesn't mitigate it either. See `docs/settings.md` §8, §10.

**`set_camera_format(codec, variant, resolution, fps)`** orchestrates the
three methods above from one combination, so a caller doesn't need to know
which packet does which part. It sequences `set_video_format` →
`set_codec_quality` → `set_recording_format`, adding no verification of
its own — a failure at any step raises from that step and later steps
don't run. Its only real logic is choosing what to pass `set_video_format`:
the caller's real target resolution when a `dimension_enum` is known for
it, or (currently only 4K DCI/ProRes) the pixel-dimension-closest
resolution that *does* have one, via a private `_closest_reachable_resolution`
helper — `set_recording_format`'s closing call still targets the caller's
real resolution either way, since that packet encodes raw width/height
rather than a codec-locked enum. Full design rationale and the real-hardware
evidence behind the two-step workaround: `docs/settings.md` §9.

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

### Camera-initiated stop detection

See `docs/recording.md`'s "Camera-initiated stop detection" section for the
full real-hardware story (a slow SD card causing the camera to auto-stop
recording). Summary of the `CameraSession` surface it adds:

```python
session.is_recording        # bool | None — notification-derived, never inferred
session.last_stop_reason     # "requested" | "unexpected" | None
session.last_stop_signal     # "low_write_margin" | None — see below
session.write_margin_window_s  # float, default 2.0
await session.wait_while_recording(timeout)  # -> bool; False = stopped early
```

`_handle_incoming` (the real `INCOMING_CONTROL` subscribe callback, replacing
the router's `handle_incoming` directly) now feeds every notification through
the router (for the pull-based echo wait), `_observe_recording_state`, and
`_observe_write_margin` (both continuous push-based watches). `is_recording`
reflects the last-reported state from *any* recording-category notification,
not just ones a pending `record_start()`/`record_stop()` call is waiting on.
A `True → False` transition observed while no such call is in flight
(tracked via `_pending_command`) is an unexpected stop: `last_stop_reason`
becomes `"unexpected"`, `last_stop_timecode` is snapshotted (so
`last_clip_duration_seconds()` still works), and callers blocked in
`wait_while_recording()` return `False` immediately.

`_observe_write_margin` separately tracks a CANDIDATE storage signal (see
`docs/recording.md`'s "A CANDIDATE signal: the write-margin warning") that's
been observed to precede that same kind of unexpected stop on real
hardware. If a low-margin reading was seen within `write_margin_window_s`
of an unexpected stop, `last_stop_signal` is set to `"low_write_margin"` —
a separate attribute from `last_stop_reason`, deliberately: folding this
into `last_stop_reason` as a compound string would silently break any
caller doing `if session.last_stop_reason == "unexpected":`, exactly the
case where the extra detail matters most. `last_stop_signal` stays `None`
for every requested stop and for an unexpected stop with no preceding
warning — it never claims a specific cause without direct supporting
evidence for that particular stop. A stale reading from a *previous* clip
can't leak into the next one either: `_observe_recording_state` resets the
tracked timestamp on every fresh `False → True` recording transition.

`record_stop()` is a no-op when `is_recording` already positively confirms
`False` (an already-stopped camera doesn't echo a redundant stop command —
see `docs/recording.md` — so attempting one would otherwise raise a
misleading `BMDVerificationError`).

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
  storage state before recording requires notification-driven storage
  state tracking, which doesn't exist yet (no card-ready check, no
  remaining-capacity tracking, no `BMDStorageError` gating). Not
  implemented. Note: `is_recording`/`last_stop_reason`/`last_stop_signal`
  are a small, notification-driven slice of what CLAUDE.md's planned
  `CameraState` (design principle 4) would eventually cover — they live as
  plain attributes on `CameraSession` for now rather than a separate
  `state.py`/`CameraState` object, since recording is still the only
  category implemented.
- **GAP/device metadata passthrough** — `camera_controller.py` already
  exposes `read_gap_identity_metadata`/`read_device_information_metadata`;
  `CameraSession` doesn't wrap them yet.
- **Reconnect wiring beyond `camera_controller.connect()`'s own behavior** —
  no additional session-level retry/backoff logic.
- **Any category other than recording (and one CANDIDATE storage signal)** —
  `CameraSession` only has `record_start`/`record_stop` today.
- **Confirmed causation for an unexpected stop.** `last_stop_signal ==
  "low_write_margin"` reports a correlated CANDIDATE signal, not a decoded
  "reason code" — it hasn't been isolated from other possible autostop
  causes (card full, card removed, power loss). See `docs/recording.md`.

---

## Testing

`tests/unit/test_notification_router.py` covers `arm`/`wait_for` timing
(match delivered before vs. after `wait_for` is called, timeout returns
`None`, `arm` rejects a match already buffered before it was called), that
undecodable notifications are ignored, a regression test for the
duplicate-retransmit race described above (a stale retransmit already
visible at `arm()` time must not satisfy `wait_for`), and a regression test
for the arm-time-snapshot fix (a fresh echo must be accepted even if its
value matches an older, unrelated, never-consumed delivery).
`tests/unit/test_session.py` mocks `BMDCameraController` and
`NotificationRouter` to cover: success on a matching echo, `BMDVerificationError`
on timeout, `BMDVerificationError` on a mismatched confirmed state, missing
profile command/values raising before any write is attempted, TIMECODE
snapshot capture (including that a failed verification snapshots nothing,
and that no TIMECODE reading yet yields `None`), `last_clip_duration_seconds()`,
the post-connect settle wait, camera-initiated stop detection
(`_observe_recording_state` updating `is_recording` and flagging/not-flagging
an unexpected stop depending on `_pending_command`, `wait_while_recording()`
returning `True`/`False`, and `record_stop()`'s no-op guard), and the
write-margin signal (`_observe_write_margin` tracking a low-margin reading;
`last_stop_signal` set only when that reading falls within
`write_margin_window_s` of an unexpected stop, staying `None` for a
requested stop, a stop with no preceding warning, or a reading outside the
window; and the timestamp resetting on a fresh recording start). No real
BLE in either test file. `tests/unit/protocol/categories/test_storage.py`
covers `is_storage_notification`/`decode_write_margin` directly.
`tests/unit/test_timecode.py` covers `decode_timecode`/`duration_seconds`
directly — see `docs/timecode.md`.
