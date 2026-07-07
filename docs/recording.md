# Recording (Record Start / Stop)

## Overview

`protocol/categories/recording.py` implements packet encoding/decoding for the
recording command family — record start and record stop. It has no BLE
knowledge and no model-specific knowledge: category, parameter, data type,
reserved byte, and payload value are all supplied by the caller from a
`CameraProfile`, never hardcoded in this module (CLAUDE.md design principle 1:
no hardcoded protocol values).

**Status: category/parameter/payload values populated, hardware round-trip
not yet confirmed.** `POCKET_6K_G2 v7.9`'s recording category, parameter,
data type, reserved byte, and start/stop payload values are reverse-engineered
and byte-level cross-validated against a known real command
(`FF 05 00 01 0A 01 01 00 02` for start, `...00 00` for stop) — see
`payloads/models/POCKET_6K_G2_v7.9.json`. No live hardware round-trip (an
actual `INCOMING_CONTROL` echo observed after sending the command) has
confirmed them yet, so the profile's `_meta.status` stays `UNVERIFIED`. See
"Open question" and "Remaining work" below.

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

`decode_recording_state` is unaffected by this: nonzero-vs-zero truthiness
still correctly distinguishes recording (`2`, truthy) from stopped (`0`,
falsy), so no change was needed there.

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

### Open question: the echo has not yet been observed

A real capture session (`tools/sniffers/sniffer_recording.py`, run while the
operator triggered record start/stop separately) did not show any
`INCOMING_CONTROL` notification with category `0x0A` in either capture
window — the categories that did appear (`0x01`, `0x03`, `0x07`, `0x09`,
`0x0C`) look like general status telemetry unrelated to recording. This may
mean: the command wasn't actually triggered via BLE during that capture
window; the camera doesn't echo this specific category the way step 3 above
assumes; or recording state is only observable via `CAMERA_STATUS` or some
other signal. This is unresolved — do not assume step 3's echo-based
verification works until a future sniffer run confirms what the real echo
(if any) looks like.

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
   parameter, and payload bytes.~~ Done — but see "Open question" above: the
   values below came from reverse-engineering + byte-level cross-validation,
   not from a captured `INCOMING_CONTROL` echo, since no echo was observed.
2. ~~Add the confirmed values to `payloads/models/POCKET_6K_G2_v7.9.json`.~~
   Done (`recording.category=10`, `parameter=1`, `data_type="BOOL"`,
   `reserved=1`, `start_value=2`, `stop_value=0`).
3. ~~Extend `CameraProfile` with accessors for the new `recording` JSON
   fields.~~ Done (`recording_category`, `recording_parameter`,
   `recording_data_type`, `recording_reserved`, `recording_start_value`,
   `recording_stop_value`).
4. Run another sniffer session that actually writes the command to
   `OUTGOING_CONTROL` (the current sniffer only listens — it does not send)
   while capturing `INCOMING_CONTROL`, to determine what a real echo for this
   category looks like, or confirm there isn't one.
5. Wire `encode_record_start` / `encode_record_stop` /
   `is_recording_state_echo` / `decode_recording_state` into `session.py`
   with the dual-check verification strategy described above, once step 4
   resolves the open question.
6. Test on real hardware, then mark the profile `VERIFIED`.
