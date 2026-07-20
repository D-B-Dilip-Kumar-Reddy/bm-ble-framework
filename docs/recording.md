# Recording (Record Start / Stop)

**Status:** implemented and hardware-verified on `POCKET_6K_G2 v7.9` (echo-verified 3/3 cycles); storage preconditions are still planned.

## Overview

`protocol/categories/recording.py` implements packet encoding/decoding for the
recording command family — record start and record stop. It has no BLE
knowledge and no model-specific knowledge: category, parameter, data type,
reserved byte, and payload value are all supplied by the caller from a
`CameraProfile`, never hardcoded in this module (CLAUDE.md design principle 1:
no hardcoded protocol values).

**Status: record start/stop verification confirmed on real hardware.**
`POCKET_6K_G2 v7.9`'s recording category, parameter, data type, reserved
byte, and start/stop payload values are reverse-engineered and byte-level
cross-validated against a known real command
(`FF 05 00 01 0A 01 01 00 02` for start, `...00 00` for stop) — see
`payloads/models/POCKET_6K_G2_v7.9.json`. `examples/record_start_stop.py`
has since run `CameraSession`'s echo-based verification across 3 repeated
start/stop cycles on real hardware, all 3/3 confirmed — this is no longer
just a byte-level cross-check, it's a live, repeatable confirmation that the
command and its echo work as understood.

The profile's `_meta.status` intentionally stays `UNVERIFIED`: that flag
describes the *whole* profile, and only the recording category has been
implemented and tested so far — settings, media, and metadata are still
unbuilt. See "Remaining work" below.

---

## Module contents (`protocol/categories/recording.py`)

| Function | Purpose |
|---|---|
| `encode_record_start(*, category, parameter, data_type, value, reserved=RESERVED_BYTE)` | Encodes a record-start command packet with the given payload `value`. |
| `encode_record_stop(*, category, parameter, data_type, value, reserved=RESERVED_BYTE)` | Encodes a record-stop command packet with the given payload `value`. |
| `is_recording_state_echo(header, *, category, parameter)` | Returns whether a decoded `INCOMING_CONTROL` packet header matches the recording state category/parameter — used to pick the echo out of the notification stream before decoding it. |
| `decode_recording_state(payload, data_type)` | Decodes an echo payload into a `bool` (`True` = recording, `False` = stopped). |

Both encoders build an `ASSIGN` operation (`protocol/codec.py`) — recording
start/stop is a state assignment, not a relative offset. `command_id` is
`0x00`, matching every other write command in this repo's test fixtures.

**Real hardware does not use a plain boolean 0/1 payload.** `POCKET_6K_G2
v7.9`'s reverse-engineered command uses `2` for start and `0` for stop, tagged
with data-type byte `0x01` (`INT8` under the official coding — SDI
transport-mode semantics, see `docs/protocol.md` §6). `value: int` is therefore an explicit, required
parameter sourced from the profile's `commands.recording.values` map
(`{"start": 2, "stop": 0}` — see `docs/payload_profiles.md`), never assumed
to be `1`/`0`. Likewise `reserved` is explicit (default `RESERVED_BYTE`)
since the real captured command uses `reserved=0x01`, not the codec's generic
`0x00` default — `session.py` passes `CommandSpec.reserved` through.

`decode_recording_state` still distinguishes recording (truthy) from stopped
(falsy) via nonzero-vs-zero, but its payload-width check is "at least
`width` bytes," not exact — a real echo's payload is longer than the nominal
`INT8` width (see below), so only the leading byte is read.

Only `DataType` values present in `DATA_TYPE_STRUCT_FORMATS`
(`protocol/types.py`) are supported; the functions raise `ValueError` for
unsupported types (e.g. `STRING`, `VOID`) rather than guessing an encoding.

---

## Verification strategy (per CLAUDE.md design principle 3)

Record start/stop is a write command, so it must be verified, not assumed:

1. Subscribe to `INCOMING_CONTROL` and start buffering **before** sending the
   command (`NotificationRouter.arm()` — see `docs/session_and_verification.md`).
2. Send the encoded packet from `encode_record_start` / `encode_record_stop`
   to `OUTGOING_CONTROL`.
3. Await a decoded packet where `is_recording_state_echo(header, category=..., parameter=...)`
   is `True`; decode its payload with `decode_recording_state`.
4. Optionally cross-check against a `CAMERA_STATUS` read. On
   `POCKET_6K_G2 v7.9`, `CAMERA_STATUS` notifications are unreliable, so the
   echo is the primary source of truth and the status read is secondary only.
5. If neither the echo nor the status cross-check confirms the expected
   `is_recording` state within the configured timeout, raise
   `BMDVerificationError` — never report success on an unconfirmed write.

`CameraState.is_recording` (planned — no `CameraState` exists yet, see
CLAUDE.md design principle 4) must only ever be updated from this decoded
echo (or a `CAMERA_STATUS` notification), never inferred from "the command
was sent successfully."

### The echo has been observed

A later capture session (`tools/sniffers/sniffer_recording.py`, after fixing
a codec bug that had been rejecting every real notification — see
`docs/packet_structure_and_constants.md`) found exactly one
`INCOMING_CONTROL` notification with category `0x0A` / parameter `0x01` in
the `record_start` window, and two (a likely retransmit) in `record_stop` —
unlike other categories in the same capture (`0x09`, `0x0C`), which fire
continuously (~1/sec, clearly ambient telemetry unrelated to recording), this
one fires only once per window, consistent with a discrete state-change
notification.

The two payloads:
- record_start: `02 00 40 00 01 03`
- record_stop:  `00 00 40 00 01 03`

Only the **leading byte** differs (`0x02` vs `0x00`) — exactly matching the
command's own payload convention (`start_value=2`, `stop_value=0`). The
trailing 5 bytes (`00 40 00 01 03`) are identical in both captures; their
meaning is **not understood yet** (not modeled in the profile JSON — only
`echo_operation` is, since that's understood well enough to be useful).

The echo also uses a different `Operation` value than the command:
`operation=0x02`, sniffer-verified and added to `protocol/codec.py` as
`Operation.CAMERA_REPORT` (the command itself uses `ASSIGN=0x00`). This is
now stored as `commands.recording.echo_operation` in the profile JSON and
surfaced as `CommandSpec.echo_operation`.

This was found via **passive listening only** across two independent
captures — no command was sent by this repo's tooling; the operator
triggered start/stop out-of-band. `tools/control/send_record_command.py`
(see `docs/active_camera_control.md`) has since closed this gap: a real run
sent `record_start`/`record_stop` directly and captured the **same** echo
signature as the very first notification after each write (payload leading
byte 2/0, `CAMERA_STATUS` also observed for the first time, value `0x03`) —
this is no longer just a coincidental passive observation, it's a
deterministic, on-demand confirmation.

---

## Storage preconditions (per CLAUDE.md design principle 10)

*(Planned — storage monitoring is not implemented yet.)* Before
encoding/sending a record-start command, the caller (`session.py`) must
check `CameraState.storage`:

- Card ready (slot present, not write-protected, not in an error state)
- Remaining recording time > 0

If either check fails, raise `BMDStorageError` immediately — do not attempt
the command. After a verified record-stop, the caller confirms storage state
updated (remaining time decreased, clip count increased) from the next
storage-related notification, not by polling.

---

## Camera-initiated stop detection

Real captures (`6K_G2_slow_write_speed_1.txt`, `6K_PRO_slow_write_speed_1.txt`
— both against an SD card too slow for the recording's write bitrate) showed
the camera autonomously stopping a recording, sending an unsolicited
`INCOMING_CONTROL` `CAMERA_REPORT` on the recording `(category, parameter)`
— the *same* notification shape as a normal record_stop echo — without this
repo's code ever sending a stop command. In both captures this happened
within about a second of `record_start` confirming, and every `record_stop()`
call later in that cycle then got **no echo at all**: the camera was already
stopped, so a redundant stop command apparently isn't re-echoed.

Two problems followed from this, both now fixed:

**Problem 1 — nothing noticed the stop until the full recording duration had
elapsed.** `CameraSession` only watched the notification stream for a fresh
echo *while a `record_start()`/`record_stop()` call was actively waiting for
one* (via `NotificationRouter.arm()`/`wait_for()`). Anything arriving outside
that window — like this unsolicited stop, arriving in the middle of a
script's `asyncio.sleep(RECORD_SECONDS)` — was buffered but nothing ever
looked at it until the *next* explicit call.

Fixed with `_observe_recording_state()`: `CameraSession._handle_incoming`
(the real `INCOMING_CONTROL` callback, subscribed in `__aenter__`) now feeds
*every* notification through this method in addition to the router, decoding
any recording-category report and updating `is_recording` unconditionally —
this is a notification-driven read, never inferred from "we sent a command"
(CLAUDE.md design principle 4). If it observes a `True → False` transition
while no `record_start()`/`record_stop()` call is currently awaiting its own
echo (tracked via `_pending_command`, set only for the duration of
`_set_recording_state`'s own write+wait), that's classified as **unexpected**:
`last_stop_reason` is set to `"unexpected"`, `last_stop_timecode` is
snapshotted (mirroring what a requested stop does, so
`last_clip_duration_seconds()` stays meaningful), and `_unexpected_stop_event`
is set.

`CameraSession.wait_while_recording(timeout)` replaces a blind
`asyncio.sleep(duration)`: it returns `True` if `timeout` elapses with
recording still going, `False` immediately if an unexpected stop is
observed first — so a script can react right away instead of waiting out
the rest of a recording that already ended. `examples/record_start_stop.py`
uses this instead of `asyncio.sleep(RECORD_SECONDS)`.

**Problem 2 — the now-redundant `record_stop()` call raised
`BMDVerificationError` for a misleading reason.** Once the camera has
already autonomously stopped, sending another stop command gets no echo at
all (see above), so the old code always raised `no echo received within
{echo_timeout_s}s` — technically true, but misleading: it reads as "we don't
know whether the stop succeeded," when the confirmed state is actually "the
camera already stopped, on its own, for an as-yet-unknown reason." Fixed:
`record_stop()` is now a no-op when `is_recording` already positively
confirms `False` — the requested end state is already true, verified from a
notification, so there's nothing to send or wait for.

### A CANDIDATE signal: the write-margin warning

The recording category's own echo payload carries no cause — it's the same
single truthy/falsy byte in both the requested and unexpected cases. But a
byte-level comparison of the slow-write-speed captures against 7 unrelated
normal-session captures (same two camera models) found a second, distinct
notification that reliably precedes the autonomous stop:

```
FF 07 00 00 09 01 01 02 00 FE 00
```

Decoded: `category=0x09`, `parameter=0x01`, `data_type=0x01` (`INT8`),
`operation=0x02` (`CAMERA_REPORT`), 3-byte payload `00 FE 00`. The payload's
**offset-1 byte** flips from `0x01` (`1`, "nominal") to `0xFE` (`-2`,
"low_margin") **0.1–1.4 seconds before every autonomous stop observed** —
6/6 occurrences, 3 start/stop cycles × 2 camera models
(`POCKET_6K_G2 v7.9`, `POCKET_6K_PRO v8.6`). Across the 7 normal sessions,
this notification fires exactly once per session and is always `0x01` —
`0xFE` never appears. Payload offsets 0 and 2 are constant (`0x00`) in every
sample, both conditions; only offset 1 carries information observed so far.

This is now modeled as `payloads/models/*.json`'s `storage.write_margin_warning`
block (`category=9, parameter=1, data_type=INT8, byte_offset=1,
values={"nominal": 1, "low_margin": -2}`, `provenance.status: "CANDIDATE"`),
decoded via `protocol/categories/storage.py`'s `decode_write_margin`, and
watched continuously by `CameraSession._observe_write_margin` the same way
`_observe_recording_state` watches the recording category — every
`INCOMING_CONTROL` notification, not just ones an active call is waiting on.

If a low-margin reading (`value == spec.values["low_margin"]`, an *exact*
match — never a sign-based "any negative value" heuristic, per CLAUDE.md
design principle 6) was seen within `write_margin_window_s` (default `2.0`s,
chosen from the observed 0.1–1.4s window) before an unexpected stop,
`CameraSession.last_stop_signal` is set to `"low_write_margin"`. Otherwise
it stays `None` — including for every *requested* stop, and for an
unexpected stop with no preceding warning. **`last_stop_signal` is a
separate attribute from `last_stop_reason`**, which keeps its existing,
documented `"requested" | "unexpected" | None` contract unchanged; a caller
wanting the detail checks both:

```python
if session.last_stop_reason == "unexpected":
    if session.last_stop_signal == "low_write_margin":
        ...  # likely a slow SD card
    else:
        ...  # stopped for an unknown reason
```

**What this is not:** confirmed causation, or CLAUDE.md's planned "Storage
Media Monitoring" subsystem. There is no log of a *different* autostop cause
(card full, card removed, power loss) to rule those out — attributing this
signal to "slow write speed" specifically is inference from test context (a
known-slow card was used in both captures), not something read off the wire
alone. This also doesn't implement storage-precondition gating — no
card-ready check, no remaining-capacity tracking, no `BMDStorageError`. See
"Remaining work" below for the real-hardware follow-up that could resolve
this.

**A one-off sighting outside this context (2026-07-20, not treated as
evidence):** a `send_settings_command.py --packet recording_format`
capture (`docs/settings.md` §6) contained one `category=0x09 parameter=0x01`
report reading `-2`/`low_margin`, with no recording in progress. This is
*not* added to the correlation above: that capture window is confounded —
it was taken before a connect-settle fix, so it's largely the camera's
post-connect initial-payload burst rather than a response to anything
this repo's tooling did, and the burst appears to include a full sweep of
ambient state (this signal among many others) regardless of context. Worth
re-checking if the signal turns up again in a clean (post-settle) capture,
but a single contaminated-window sighting is not enough to broaden the
"precedes a camera-initiated stop" correlation to "precedes any settings
change."

**Follow-up, now in clean captures (2026-07-20, `docs/settings.md` §7):**
it did turn up again — repeatedly. A 16-run `--dimension-enum` probe
sweep, taken entirely *after* the connect-settle fix above (so these
captures are genuine responses, not burst noise), read `low_margin`/`-2`
in essentially every one of its ~18 connect cycles across roughly 15
minutes, with no recording ever active. That's a much longer, steadier
persistence than the signal's original evidence (a brief 0.1–1.4s pre-stop
warning). This doesn't disprove the original correlation — no autostop
happened during the sweep either, so "low_margin" and "about to stop" are
still not shown to be the same thing — but it does undercut reading
`low_margin` as *itself* a short-fused warning. A steadier explanation fits
better: this signal may reflect a per-(SD card, resolution/bitrate)
threshold that this particular card sits below at the BRAW resolutions the
sweep used, largely independent of whether anything is recording. Recorded
in the profile's `storage.write_margin_warning.provenance.notes`; no
change to `values` or to `CameraSession`'s behavior — the signal still
only feeds `last_stop_signal` around an actual stop, and this doesn't
change that logic, only the confidence in what a lone `low_margin` reading
means in isolation.

---

## Remaining work

Per CLAUDE.md, "Workflow: Adding a New Command":

1. ~~Run `tools/sniffers/sniffer_recording.py` to capture real category,
   parameter, and payload bytes.~~ Done.
2. ~~Add the confirmed values to `payloads/models/POCKET_6K_G2_v7.9.json`.~~
   Done (`commands.recording`: `category=10`, `parameter=1`,
   `data_type="INT8"`, `reserved=1`, `values={"start": 2, "stop": 0}`,
   `echo_operation=2` — see `docs/payload_profiles.md` for the structure).
3. ~~Extend `CameraProfile` with accessors for the new `recording` JSON
   fields.~~ Done — `profile.require_command("recording", ("start", "stop"))`
   returns a `CommandSpec` carrying the whole block.
4. ~~Determine what a real `INCOMING_CONTROL` echo for this category looks
   like.~~ Done via passive listening — see "The echo has been observed."
5. ~~Build a send-then-observe mode, and confirm the echo on real
   hardware.~~ Done — `tools/control/send_record_command.py` (see
   `docs/active_camera_control.md`) sent `record_start`/`record_stop`
   directly on real hardware and captured the same echo signature already
   found passively, deterministically. The 5 trailing payload bytes remain
   unexplained — a future investigation, not blocking verification (only the
   leading byte is used).
6. ~~Wire `encode_record_start` / `encode_record_stop` /
   `is_recording_state_echo` / `decode_recording_state` into `session.py`.~~
   Done — see `docs/session_and_verification.md` and
   `examples/record_start_stop.py`. Verification is echo-only (not the
   documented echo+`CAMERA_STATUS` dual-check) since `CAMERA_STATUS`'s known
   bits don't cover recording state — see that doc for why.
7. ~~Test on real hardware.~~ Done — `examples/record_start_stop.py`
   confirmed 3/3 start/stop cycles via echo verification on a real
   `POCKET_6K_G2 v7.9`. The profile's `_meta.status` stays `UNVERIFIED`
   overall (only recording is implemented; settings/media/metadata aren't
   yet) — recording's own verification is recorded as structured
   `commands.recording.provenance` data (`status: "VERIFIED"`, method,
   capture refs, date) in `payloads/models/POCKET_6K_G2_v7.9.json`.
8. ~~Capture the clip length.~~ Done — `CameraSession` tracks the `TIMECODE`
   reading around a confirmed `record_start`/`record_stop` and exposes
   `last_clip_duration_seconds()` (see `docs/timecode.md`). The real wire
   format (a wrapped BMD-style packet, not a bare BCD value) was confirmed
   against real captures on both `POCKET_6K_G2 v7.9` and `POCKET_6K_PRO
   v8.6`. Those same captures confirmed TIMECODE resets to `00:00:00:00`
   when recording starts, so `record_start` sets a canonical zero rather
   than snapshotting the (often stale, left over from the previous clip)
   latest reading — see `docs/timecode.md` for the bug this fixed. Duration
   is still hours/minutes/seconds precision only: the `frames` field is
   decoded and displayed but not yet used in the math, since its rollover
   point isn't confirmed to hold across frame rates. **Next**: capture a
   longer recording, or one at a different frame rate, to confirm the
   `frames` rollover and extend `duration_seconds` to be frame-accurate if
   warranted.
9. ~~Detect a camera-initiated (unexpected) recording stop.~~ Done — see
   "Camera-initiated stop detection" below.
10. **Isolate the write-margin warning from other autostop causes.** The
    `storage.write_margin_warning` CANDIDATE signal (see "A CANDIDATE
    signal: the write-margin warning" above) is real and repeatable, but
    only compared against slow-write-speed failures vs. normal successful
    recordings — not against a *different* autostop cause. Next: a
    real-hardware session that deliberately induces a card-full stop and/or
    a card-removed stop, capturing the same way, to check whether the
    signal is specific to write speed or a more general "recording ended
    abnormally" indicator. Out of scope for this repo's automated work — no
    live camera in this environment; a human operator needs to run the
    repro and share the logs, same iterative loop as every other protocol
    finding in this doc.
