# Recording (Record Start / Stop)

## Overview

`protocol/categories/recording.py` implements packet encoding/decoding for the
recording command family — record start and record stop. It has no BLE
knowledge and no model-specific knowledge: category, parameter, and payload
data type are supplied by the caller from a `CameraProfile`, never hardcoded
in this module (CLAUDE.md design principle 1: no hardcoded protocol values).

**Status: scaffold only.** No sniffer capture in this repo has yet confirmed
the recording category, parameter, or data type for `POCKET_6K_G2 v7.9` or
any other camera. The functions below are ready to use once those values are
captured — see "Remaining work" for the exact steps.

---

## Module contents (`protocol/categories/recording.py`)

| Function | Purpose |
|---|---|
| `encode_record_start(*, category, parameter, data_type)` | Encodes a record-start command packet (payload = truthy value for `data_type`). |
| `encode_record_stop(*, category, parameter, data_type)` | Encodes a record-stop command packet (payload = falsy value for `data_type`). |
| `is_recording_state_echo(header, *, category, parameter)` | Returns whether a decoded `INCOMING_CONTROL` packet header matches the recording state category/parameter — used to pick the echo out of the notification stream before decoding it. |
| `decode_recording_state(payload, data_type)` | Decodes an echo payload into a `bool` (`True` = recording, `False` = stopped). |

Both encoders build an `ASSIGN` operation (`protocol/codec.py`) — recording
start/stop is a state assignment, not a relative offset. `command_id` is
`0x00`, matching every other write command in this repo's test fixtures.

Only `DataType` values present in `DATA_TYPE_STRUCT_FORMATS`
(`protocol/types.py`) are supported — `BOOL` is expected for a simple
start/stop flag once confirmed by sniffer, but the functions do not assume it
in advance; they raise `ValueError` for unsupported types (e.g. `STRING`,
`VOID`) rather than guessing an encoding.

---

## Verification strategy (per CLAUDE.md design principle 3)

Record start/stop is a write command, so it must be verified, not assumed:

1. Subscribe to `INCOMING_CONTROL` and start buffering **before** sending the
   command (`NotificationRouter`).
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

This module is intentionally incomplete until real hardware data exists.
Per CLAUDE.md, "Workflow: Adding a New Command":

1. Run `tools/sniffers/` (not yet implemented) while starting and stopping a
   recording on `POCKET_6K_G2 v7.9` to capture the real category, parameter,
   and payload bytes for both start and stop.
2. Add the confirmed values to `payloads/models/POCKET_6K_G2_v7.9.json`
   (e.g. under a `recording` key — category, parameter, data type) — never
   hardcode them in `recording.py` or copy them from another model's profile.
3. Extend `CameraProfile` with accessors for the new `recording` JSON fields.
4. Wire `encode_record_start` / `encode_record_stop` /
   `is_recording_state_echo` / `decode_recording_state` into `session.py`
   with the dual-check verification strategy described above.
5. Add the `recording` row to the "Command categories" table in CLAUDE.md.
6. Test on real hardware, then mark the profile `VERIFIED`.

Until step 1 happens, do not add recording fields to any profile JSON, and do
not mark a profile `VERIFIED` on the basis of this scaffold alone.
