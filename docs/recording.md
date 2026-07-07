# Recording (Record Start / Stop)

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
with `data_type=BOOL`. `value: int` is therefore an explicit, required
parameter sourced from the profile (`profile.recording_start_value` /
`profile.recording_stop_value`), never assumed to be `1`/`0`. Likewise
`reserved` is explicit (default `RESERVED_BYTE`) since the real captured
command uses `reserved=0x01`, not the codec's generic `0x00` default —
`profile.recording_reserved` should be passed through once `session.py`
wires this up.

`decode_recording_state` still distinguishes recording (truthy) from stopped
(falsy) via nonzero-vs-zero, but its payload-width check is "at least
`width` bytes," not exact — a real echo's payload is longer than the nominal
`BOOL` width (see below), so only the leading byte is read.

Only `DataType` values present in `DATA_TYPE_STRUCT_FORMATS`
(`protocol/types.py`) are supported; the functions raise `ValueError` for
unsupported types (e.g. `STRING`, `VOID`) rather than guessing an encoding.

---

## Verification strategy (per CLAUDE.md design principle 3)

Record start/stop is a write command, so it must be verified, not assumed:

1. Subscribe to `INCOMING_CONTROL` and start buffering **before** sending the
   command (`NotificationRouter`, not yet implemented).
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

`CameraState.is_recording` must only be updated from this decoded echo (or a
`CAMERA_STATUS` notification), never inferred from "the command was sent
successfully."

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
now stored as `recording.echo_operation` in the profile JSON and
`CameraProfile.recording_echo_operation`.

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

Before encoding/sending a record-start command, the caller (`session.py`,
once implemented) must check `CameraState.storage`:

- Card ready (slot present, not write-protected, not in an error state)
- Remaining recording time > 0

If either check fails, raise `BMDStorageError` immediately — do not attempt
the command. After a verified record-stop, the caller confirms storage state
updated (remaining time decreased, clip count increased) from the next
storage-related notification, not by polling.

---

## Remaining work

Per CLAUDE.md, "Workflow: Adding a New Command":

1. ~~Run `tools/sniffers/sniffer_recording.py` to capture real category,
   parameter, and payload bytes.~~ Done.
2. ~~Add the confirmed values to `payloads/models/POCKET_6K_G2_v7.9.json`.~~
   Done (`recording.category=10`, `parameter=1`, `data_type="BOOL"`,
   `reserved=1`, `start_value=2`, `stop_value=0`, `echo_operation=2`).
3. ~~Extend `CameraProfile` with accessors for the new `recording` JSON
   fields.~~ Done (`recording_category`, `recording_parameter`,
   `recording_data_type`, `recording_reserved`, `recording_start_value`,
   `recording_stop_value`, `recording_echo_operation`).
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
   yet) — recording's own verification is recorded in the `recording` block's
   `_comment` in `payloads/models/POCKET_6K_G2_v7.9.json`.
