# CLAUDE.md — bm-ble-framework

## Project Overview

Python package (`bmd_ble`) for automated Blackmagic Design camera control over Bluetooth Low Energy.

**Target operations:**
- Record start / stop
- Settings changes: codec, quality, resolution, FPS
- Photo capture
- Video playback / gallery browsing — send play, pause, forward, backward commands and observe behaviour
- Video and photo metadata capture
- Storage media monitoring — SD card state, remaining space, slot status (used to diagnose recording, capture, and playback failures)

**End-user API:** Python scripts only. No CLI.

**Platform:** Windows only.

---

## Camera Registry

| Model Key | Model Name | Firmware | Status | Notes |
|---|---|---|---|---|
| `POCKET_6K_G2` | Pocket Cinema Camera 6K G2 | v7.9 | In progress | Primary reference; most reverse-engineered. **The operator's physical unit was upgraded to v8.6 on 2026-07-27** — v7.9 can no longer be tested against real hardware; further G2 work needs a new `POCKET_6K_G2_v8.6` profile scaffolded from Phase 1 (see the reverse-engineering workflow below), not assumed to inherit anything from this v7.9 profile without its own fresh sniffing (design principle 6) |
| `POCKET_6K_PRO` | Pocket Cinema Camera 6K Pro | v8.6 | In progress | Second target |
| `URSA_BROADCAST_G2` | URSA Broadcast G2 | v7.5 | Planned | Different category/param combos expected |
| `URSA_MINI_PRO_12K` | URSA Mini Pro 12K | v8.1 | Planned | Different category/param combos expected |
| `POCKET_4K` | Pocket Cinema Camera 4K | v8.6 | Planned | |

Start all new features with `POCKET_6K_G2 v7.9`. Add `POCKET_6K_PRO v8.6` second.

The `ble_name` field in every profile JSON is the real BLE advertisement name broadcast by the camera — not a placeholder.

---

## Design Principles

These principles describe the **target architecture**. Everything marked
*(planned)* here or in the Package Structure below is design intent that is not
yet implemented — the docs in `docs/` record exactly what exists today. Never
describe a planned subsystem as implemented.

### 1. No hardcoded protocol values
Codec IDs, quality variant IDs, FPS encodings, resolution encodings, category/parameter combinations — none of these belong in code. They live in `payloads/models/<MODEL_KEY>_<FIRMWARE>.json`. Code reads from the profile. The only values permitted in `constants.py` are those that are fixed by the Bluetooth spec or the BMD BLE API spec and do not vary between models.

### 2. Profile-driven behaviour
`CameraProfile` is the single source of truth for all model/firmware-specific constants. Everything the controller and protocol layer need to construct a command comes through the profile.

### 3. Verification-first writes
**Every write command must be verified before reporting success.** Silently assuming a command worked is never acceptable.

Use a dual-check strategy:
- **Primary** — await an echo on `INCOMING_CONTROL` (fast; subscribe and buffer *before* sending the command, not after)
- **Secondary** — read `CAMERA_STATUS` as a cross-check

Both checks carry configurable timeouts. If neither confirms the state change, raise `BMDVerificationError`. On `POCKET_6K_G2 v7.9`, `CAMERA_STATUS` notifications are unreliable — always attempt the echo first and treat the status read as a secondary check only.

*Current implementation status:* recording verification is **echo-only** — none of the known `CAMERA_STATUS` bits encode recording state, so there is no meaningful secondary cross-check for it yet. See `docs/session_and_verification.md`.

Photo capture (both `POCKET_6K_G2 v7.9`'s and `POCKET_6K_PRO v8.6`'s confirmed `commands.photo`, independently verified on each camera, see `docs/photo_capture.md` §7 and §9) is a harder case still unresolved: the trigger command itself is confirmed on real hardware on both cameras (verified by inspecting the SD card's contents, not any BLE signal), but **no channel at all** — neither echo nor `CAMERA_STATUS` — has ever been observed to move in response to it on either camera, on either the passive or active evidence gathered so far. This is why `CameraSession.capture_photo()` is not built yet: this principle requires every write to be confirmed before reporting success, and there is currently nothing on `INCOMING_CONTROL` or `CAMERA_STATUS` to confirm against. Resolving this (a real per-photo signal, a documented best-effort exception, or something else) is an open decision, not yet made — see `docs/photo_capture.md` §7's closing discussion.

### 4. Observable state model *(planned)*
A `CameraState` object reflects the last-known camera state. It is updated **only** from incoming BLE notifications — never inferred from "I sent command X therefore state is now Y". On connect, read the current state before any automation begins. *(The full `state.py` / `CameraState` / `StorageState` object is not yet implemented. A small, notification-driven slice of this already exists directly on `CameraSession` — `is_recording`/`last_stop_reason`, updated only from decoded recording-category notifications, used to detect a camera-initiated stop (e.g. on a slow SD card) without waiting on a command's own echo; `last_known_codec_variant`, updated only from decoded codec_quality-category notifications; and `last_known_recording_format`, updated only from decoded recording_format-category notifications (including `set_video_format`'s own mode-notify confirmations, which share that same category/parameter). All three no-op guards — `set_codec_quality`, `set_video_format`, and `set_recording_format` — use these fields to recognize an already-satisfied write and skip it instead of waiting on an echo the camera won't send for a no-op (real-hardware-confirmed for all three families, 2026-07-21) — see `docs/session_and_verification.md`, `docs/recording.md`, and `docs/settings.md`.)*

### 5. Strict transport / protocol separation
- `camera_controller.py` — BLE transport only: connect, disconnect, raw byte read/write, notification subscription. No BMD protocol knowledge.
- `protocol/` — BMD packet encoding/decoding only. No BLE knowledge.
- `session.py` — composes the two layers. This is the only surface user scripts touch.

Never mix concerns across these boundaries.

### 6. Sniffer-first for all protocol values
Every codec ID, quality variant, FPS encoding, category/parameter pair must originate from a real sniffer capture on that specific camera and firmware. Never copy protocol values from one profile to another without re-verifying on that model. `tools/sniffers/` (passive) and `tools/control/` (active send-then-capture) drive the payload population workflow.

### 7. Explicit capability model
Each profile JSON declares what the camera supports (e.g. `supports_raw`, `supports_playback`, `supports_photo`). Code checks capabilities before attempting an operation. Attempting an unsupported operation raises `BMDUnsupportedError` immediately — no silent failures.

A second, distinct flavor of this: `resolutions.<name>.known_unreachable` (codec name → evidence note) records a *software* capability gap — a `(codec, resolution)` combination the camera itself demonstrably supports, but that this codebase's write path cannot reach despite exhausting every write-value hypothesis (real example: `POCKET_6K_PRO v8.6`'s ProRes/4K DCI gap, `docs/settings.md` §16). Never remove the codec from `codecs` on the strength of a `known_unreachable` entry — the camera's own capability is unchanged; only this codebase's current write path is limited. `CameraSession.set_camera_format` checks this before any write and raises `BMDUnsupportedError` immediately, quoting the evidence note. Entries here are added only after a real investigation is exhausted (see `docs/payload_profiles.md`) — `tools/control/sweep_camera_format.py` surfaces *candidates* systematically, but a human reviews the evidence before writing the field.

A third flavor is the opposite kind of fact: `resolutions.<name>.max_fps_int` records a real *camera hardware* ceiling — the camera itself cannot exceed this fps at this resolution at all (real example: `POCKET_6K_PRO v8.6`'s `"6K"` topping out at 50fps, confirmed both by `sweep_camera_format.py`'s first production run and by the operator checking the camera's own UI — `docs/settings.md` §17). Unlike `known_unreachable`, this isn't a software gap to fix later — there's nothing to fix. `CameraSession.set_video_format` and `set_recording_format` both check this before any write (not just the `set_camera_format` orchestration, since both take `(resolution, fps)` directly) and raise `BMDUnsupportedError` immediately for a requested fps above the ceiling. `sweep_camera_format.py` excludes fps values above a resolution's ceiling from its default sweep for the same reason it excludes `known_unreachable` combinations. That same first production run also demonstrated a real methodological hazard worth remembering for any sweep tool: one `unconfirmed` result turned out to be a false negative in the tool's own default echo timeout (confirmed successful on-screen despite reporting failure) — an `unconfirmed` outcome is evidence about that run's timing, not automatically evidence about the camera; check the on-screen state before trusting it as a `known_unreachable`/`max_fps_int` candidate. A same-shape hazard in the opposite direction hit `sweep_dimension_enum.py` on `POCKET_6K_PRO v8.6` (2026-07-27, `docs/photo_capture.md` §10.5): a candidate that looked like a genuine `MATCH` turned out to be a false positive — leftover state from before the write, not a result the candidate caused, exposed only by an immediate repeat run giving a different answer. A `MATCH`, like an `unconfirmed`, is not automatically evidence about the camera either; the tool now tracks and flags this specific failure mode directly.

### 8. Fail loud on unverified profiles
If `status == "UNVERIFIED"` in a profile JSON, log a prominent warning at session start. Code can still run against unverified profiles, but the user must know.

### 9. Graceful degradation for reads only
**Reads** (metadata, GAP info, device info) are best-effort — return `None` on failure, do not raise. **Writes** are verification-first — they must confirm success or raise. Write degradation is never silent.

### 10. Storage-aware operations
Before recording or photo capture, verify the storage media is ready (card inserted, not full, not in an error state). After recording or capture, confirm storage state has updated (e.g. remaining space decreased, clip count increased). Storage state is part of `CameraState` and is updated from notifications — never polled in a loop.

If storage state is unknown or unhealthy at operation time, raise `BMDStorageError` before attempting the command. Do not let the camera silently fail to save media.

### 11. Async-first
All I/O is async. No blocking calls anywhere. Use `asyncio.wait_for()` for all timeouts.

---

## Package Structure

Entries marked *(planned)* do not exist yet — they are the target layout for
future subsystems. Everything else is implemented and on disk today.

```
src/bmd_ble/
  __init__.py               # Public API surface — exports CameraSession, CameraProfile,
                            # get_profile, KNOWN_PROFILES, BMDVerificationError,
                            # BMDUnsupportedError
  constants.py              # BLE UUIDs and timing constants (fixed by spec)
  exceptions.py             # BMDConnectionError, BMDTimeoutError, BMDCommandError,
                            # BMDVerificationError, BMDUnsupportedError, BMDStorageError
                            # (BMDVerificationError and BMDUnsupportedError are raised
                            # today — the latter by the settings writes; the rest are
                            # reserved for the planned subsystems that will use them)
  scanner.py                # BLE discovery by advertisement name
  camera_profile.py         # Load, validate, and cache model/firmware profiles
  camera_controller.py      # BLE transport layer — raw bytes only
  notification_router.py    # Buffer and route INCOMING_CONTROL notifications by (category, param)
  timecode.py               # TIMECODE characteristic decode + clip-duration math
                            # (wrapped BMD packet, distinct characteristic — see docs/timecode.md)
  state.py                  # (planned) CameraState + StorageState dataclasses —
                            # updated from notifications only
  session.py                # CameraSession context manager — user-facing API
  protocol/
    __init__.py
    codec.py                # BMD packet header encode / decode
    types.py                # BMD data type constants (official spec coding — see
                            # "Data types" below)
    categories/
      __init__.py
      recording.py          # Record start / stop
      storage.py            # Passive decode of storage-monitoring
                            # notifications (CANDIDATE write-margin signal)
      settings.py           # Codec/quality, video format (codec-family switch),
                            # recording format (resolution + FPS) — values from
                            # an external RE doc, all VERIFIED on real
                            # POCKET_6K_G2 v7.9 hardware, see docs/settings.md
      media.py              # (planned) Photo capture, playback controls
      metadata.py           # (planned) Video / photo metadata reads

tools/
  common/                   # Shared BLE capture/decode engine (tools/common/capture.py)
                            # and guided-discovery logic (tools/common/discovery.py),
                            # used by both sniffers/ and control/ — not an entrypoint itself
  sniffers/                 # Passive BLE-notification capture for reverse engineering (listen-only)
  control/                  # Active camera control — sends commands, captures the response
                            # (changes real camera state; use deliberately)
  query/                    # Read-only characteristic inspection
  captures/                 # Runtime output of sniffers/ and control/ scripts (gitignored)

Tools are grouped by folder according to what kind of thing they do — read-only
query, passive listen, or active send — not by feature. Shared library code used
by more than one tool type lives in `tools/common/`, never duplicated per folder
or reached via an awkward cross-folder import. See `docs/active_camera_control.md`.

payloads/
  models/                   # One JSON file per (MODEL_KEY, firmware) pair
  schema.json               # JSON Schema — validates all payload files at load time

examples/
  scan_camera.py            # Discover cameras by BLE advertisement name
  connect_to_camera.py      # Connect-only smoke test (connect, hold, disconnect)
  monitor_incoming.py       # Stream raw INCOMING_CONTROL notifications
  record_start_stop.py      # Echo-verified record start/stop via CameraSession
  change_codec.py           # BRAW <-> ProRes round trip via set_camera_format
                            # (codec+quality+resolution+fps orchestration;
                            # see docs/settings.md and docs/session_and_verification.md)
  capture_photo.py          # (planned)
  playback.py               # (planned)

tests/
  unit/                     # No hardware, full mocking — must pass in CI
  integration/              # (planned) Mocked BLE, full command + verification round-trips
```

`session.py` is what user scripts import. Scripts never import `camera_controller`, `notification_router`, or anything from `protocol/` directly.

---

## Supplementary Documentation

### Reading order

**Before making any change to this codebase**, read this file and every file in `docs/`
to understand the current state of each subsystem. Do not rely on earlier conversation
context alone — the docs are the authoritative record of what is implemented.

### Feature doc convention

Each significant feature or subsystem has its own doc in `docs/`. When a feature is
changed, its corresponding doc must be updated in the same commit. When a new feature is
added, a new `docs/<feature>.md` must be created alongside the code change.

| File | Covers |
|---|---|
| `docs/protocol.md` | **Full protocol reference** — SDI camera control categories/parameters, data types, operations, BLE GATT layer, spec-vs-sniffer divergences. Read before any protocol work |
| `docs/packet_structure_and_constants.md` | Packet header byte layout, length-field counting base, `protocol/codec.py` design, and how the original spec-assumed structure was corrected against a real capture |
| `docs/winrt_ble_connection_hardening.md` | BLE transport reliability on Windows/WinRT — reconnect loop, liveness detection via notification timestamps, connection-generation guards, connect-lock, known limitations |
| `docs/event_subscription_and_logging.md` | Notification subscription strategy (`subscribe_all`), generation-guarding wrapper, per-session file logging |
| `docs/recording.md` | Record start/stop category scaffold, verification and storage-precondition strategy, remaining sniffer work |
| `docs/sniffer_capture_engine.md` | Reusable BLE-notification capture engine (`tools/common/capture.py`) driving labeled operator-triggered capture windows |
| `docs/active_camera_control.md` | Active camera control — `write_outgoing_control`, `run_send_and_capture`, `tools/control/` tool-type segregation |
| `docs/session_and_verification.md` | `CameraSession`, `NotificationRouter` echo buffering (`arm`/`wait_for`), why `CAMERA_STATUS` isn't a secondary cross-check for recording yet |
| `docs/payload_profiles.md` | Profile JSON structure (`commands` map, `values`, `provenance`), `payloads/schema.json` load-time validation, `CommandSpec` API |
| `docs/command_discovery.md` | Guided command discovery (`tools/control/discover_command.py`) — candidate sweep, operator confirmation, emitted profile blocks |
| `docs/timecode.md` | `TIMECODE` wire format (wrapped BMD packet, confirmed by real capture), BCD decode, clip-duration math (`timecode.py`), and why the `frames` field isn't used in duration yet |
| `docs/settings.md` | Settings families (codec/quality, video format, recording format) — byte layouts and value tables from an external RE doc, now hardware-verified for all three; why `codec_quality` can't switch BRAW↔ProRes but `video_format` can, the `0x82` data type, `set_camera_format`'s combination orchestration, and the verification runbook (`sniffer_settings.py`, `send_settings_command.py`, `change_codec.py`) |
| `docs/photo_capture.md` | Photo-capture reverse engineering — passive phase complete (2026-07-27, both cameras): a body-triggered still produces NO report at all. First active probe (G2, same day) was **inconclusive** (§6: every INT8 candidate confirmed, a sign of an unreliable read). A same-day VOID retry, this time verified via SD card contents on a PC rather than a glance, **confirmed** `commands.photo` on `POCKET_6K_G2 v7.9` (§7) — category `0x0A`/param `0x03`, void trigger, reserved byte indifferent (`0x00`/`0x01` both work) — then **independently reconfirmed identically on `POCKET_6K_PRO v8.6`** the same day (§9), same SD-card verification method. Still open on both cameras: no BLE-observable signal confirms a photo was taken (neither echo nor status), so `CameraSession.capture_photo()` isn't built yet — that verification-strategy question is the next decision. TODO (operator-proposed, not yet started): verify out-of-band over `POCKET_6K_PRO v8.6`'s USB/HTTP clip-playback interface instead of BLE — explicitly v8.6-only, picked up in a future session. §8 records operator-provided (non-wire) knowledge that BRAW stills inherit the recording resolution while ProRes stills use a separate sensor-area concept (2.8K/5.7K/6K, unrelated to ProRes's own UHD/HD video resolutions), all saved as DNG on the G2 — §8.4 corrects that DNG claim for the PRO, where file format follows the active codec (`.braw` for BRAW, DNG for ProRes). §10's first `sniffer_sensor_area.py` capture (G2, 2026-07-27) found real report activity when Sensor Area changes (`recording_format`, `codec_quality`, a capacity-shaped signal) but no directly-encoded sensor-area value on either channel — a promising, unconfirmed lead sits in the capacity signal's monotonic values; §10.2 records the operator's cross-model sensor-area matrix, including a genuine G2/PRO difference (5.7K vs 5.3K) and both cameras disabling the choice entirely at ProRes/4K DCI. §10.3's PRO rerun reached the same negative result independently, plus a genuine cross-model reconfirmation of the "windowed" flag bit tracking full-sensor-vs-cropped sensor area on both cameras — the capacity signal did not reproduce on the PRO, weakening confidence in it. §10.4's PRO-only interleaved A-B-A-B repeat (necessarily PRO-only — the operator's G2 has since been upgraded to firmware v8.6, see the Camera Registry note above) **confirmed the windowed bit as a clean, reproducible signal** (byte-identical toggle, twice each way) and put the capacity signal at a firm 0-for-2 independent PRO sessions. §10.5's PRO-only hunt for a second `dimension_enum` aliasing to `HD` found an apparent match (`0x00`) that an immediate repeat run then **refuted as a stale-state false positive** — the report simply reflected whatever the camera already held before that candidate was sent, not a result it caused; `tools/control/sweep_dimension_enum.py` now guards against this class of false positive directly (tracks the last confirmed state, flags any MATCH identical to it) |

---

## BMD BLE Protocol

Commands are written as binary packets to `OUTGOING_CONTROL`. Echoes and responses arrive on `INCOMING_CONTROL`. This section is the quick summary — `docs/protocol.md` is the full reference (all SDI categories/parameters, data-type coding discrepancy, transport-mode echo hypothesis) and must be read before any protocol work.

### Packet structure

```
Byte 0      Fixed prefix byte (0xFF — sniffer-verified on POCKET_6K_G2 v7.9,
            both directions; not a per-destination address over BLE)
Byte 1      Length field — counts only bytes 4 onwards (category, parameter,
            data type, operation, payload). Sniffer-verified: does NOT count
            command_id/reserved, unlike the generic BMD spec assumption of
            "everything after byte 1".
Byte 2      Command ID / type
Byte 3      Reserved
Byte 4      Category
Byte 5      Parameter
Byte 6      Data type  (see protocol/types.py)
Byte 7      Operation  (0x00 = assign, 0x01 = offset, 0x02 = camera report —
            sniffer-verified on every camera-originated notification
            captured so far; official spec meaning unconfirmed)
Bytes 8+    Payload
```

This structure was corrected after a real sniffer capture on `POCKET_6K_G2 v7.9`
caught a systematic decode failure — the byte 0 value and the length field's
counting base above were originally assumed from the generic BMD spec and had
never been verified against real BLE hardware. See `protocol/codec.py` and
`docs/packet_structure_and_constants.md`.

### Command categories
Populate this table as categories are confirmed from sniffer sessions. Each category maps to a file in `protocol/categories/`.

| Category | Description | File |
|---|---|---|
| `0x0A` (param `0x01`) | Recording (record start/stop) | `protocol/categories/recording.py` |
| `0x0A` (param `0x03`) | Still Capture (photo trigger) — VERIFIED on **both** `POCKET_6K_G2 v7.9` and `POCKET_6K_PRO v8.6` (2026-07-27: `tools/control/discover_command.py --data-type VOID`, operator-confirmed on each camera independently via SD card contents inspected on a PC after each send, not a wire or on-screen signal — same coordinates and same reserved-byte indifference reached independently on both). Matches the [spec] void typing (`docs/protocol.md` §5, 10.3). Reserved byte confirmed indifferent on both cameras — `0x00` and `0x01` both triggered a photo; `0x00` recorded as canonical. NO INCOMING_CONTROL notification of any kind appeared for any confirmed write on either camera, consistent with the passive finding (`docs/photo_capture.md` §5) that a body-triggered still produces no report either — there is currently no known BLE-observable signal that confirms a photo was taken, which is why `protocol/categories/media.py` and `CameraSession.capture_photo()` are not built yet despite the trigger itself being confirmed on both cameras (see `docs/photo_capture.md` §7/§9 for the open verification-strategy question this leaves, required before a session-level API can satisfy design principle 3) | `protocol/categories/media.py` (not yet built) |
| `0x0A` (param `0x00`) | Codec + quality variant — VERIFIED on `POCKET_6K_G2 v7.9` (2026-07-20: `CameraSession.set_codec_quality()` genuine real-hardware write+echo cycle); does NOT switch BRAW↔ProRes (see `docs/settings.md`). Same category/parameter and payload shape independently confirmed on `POCKET_6K_PRO v8.6` too — a real `CameraSession.set_camera_format()` write+echo cycle to ProRes/422 also confirmed cleanly there (2026-07-22, `docs/settings.md` §16), but the block stays CANDIDATE pending §16's blocking `recording_format` gap. Also used 2026-07-24 as the isolating target for an unrelated `recording_format` `Operation.OFFSET` investigation (a `+1` variant delta via `OFFSET` got zero response, same as every other `OFFSET` test on this camera — evidence toward `OFFSET` being unimplemented camera-wide, not evidence about this block's own values, see `docs/settings.md` §16) | `protocol/categories/settings.py` |
| `0x01` (param `0x00`) | Video format (FORMAT packet) — VERIFIED on `POCKET_6K_G2 v7.9` (2026-07-20: `CameraSession.set_video_format()` 2/2 real-hardware round trip); dimension_enum locks resolution + codec family, the actual BRAW↔ProRes switch. Never appears in notifications itself — enums need active probing (`docs/settings.md` §7–§8). Same coordinates confirmed working on `POCKET_6K_PRO v8.6` via an active `dimension_enum` sweep and, for the UHD/ProRes proxy resolution specifically, via a real `CameraSession` write+echo cycle too (2026-07-22, `docs/settings.md` §16) — still CANDIDATE there pending §16's blocking gap; every enum value found matches the G2's number for the same resolution (`docs/settings.md` §15) — and on that camera, the on-screen display doesn't reflect the change until a power cycle even though the write took effect. An exhaustive `0x00`–`0x16` sweep (`tools/control/sweep_dimension_enum.py`, 2026-07-22, `docs/settings.md` §16) found no ProRes/4K DCI enum in that range on the PRO either — same negative result as the G2's own exhausted search. A follow-up sweep of `0x17`–`0x1F` (2026-07-23, `docs/settings.md` §16) found nothing there either — 32 values now exhausted with no match, making further blind enum guessing a weaker lead than retrying `recording_format` with a different `data_type` byte (since ruled out too) or `video_format`'s unexplained trailing elements — also probed via `tools/control/send_settings_command.py --video-format-extra E1 E2` (added 2026-07-24) and found no support either (2026-07-24): one `(extra1, extra2)` pair was accepted but still landed UHD, three others were silently rejected. All three candidate hypotheses for this gap are now exhausted; a full-channel decode of the passive-capture evidence (§16, 2026-07-24) then found nothing new either — `recording_format` is the only channel that moves with the transition, everything else is ambient telemetry or a one-time connect-burst dump — leaving `Operation.OFFSET` (never tried; every write above used `ASSIGN`) as the one remaining untested axis — now testable via `tools/control/send_settings_command.py --operation OFFSET` (added 2026-07-24, not yet tried on real hardware); per `docs/protocol.md` §4's documented OFFSET semantics ("add the payload to the current value"), a faithful test needs the delta from the current state, not the same absolute target payload `ASSIGN` uses. Tried 2026-07-24 with the absolute target payload anyway (as a first, simpler check): zero response over a 10s window — inconclusive on `OFFSET` itself, since an absolute width sent as an `OFFSET` requests an out-of-range `current + 4096`, not a real test of the delta hypothesis. `tools/control/send_settings_command.py --raw-payload` (added 2026-07-24) then bypassed the profile's lookup tables to send a genuine `+256` width delta via `OFFSET` — and it got the identical zero-response signature (2026-07-24), stronger evidence than the absolute-payload result since the delta landed exactly in-range. Every hypothesis for this gap (enum sweep, data_type byte, trailing elements, full-channel decode, OFFSET absolute, OFFSET delta) is now exhausted with no confirming echo. A follow-up isolating test then sent an `OFFSET` delta against `codec_quality` instead (2026-07-24, `docs/settings.md` §16) — a family whose `ASSIGN` echo behavior is already well-characterized — and got silence there too, pointing at `Operation.OFFSET` being unimplemented camera-wide rather than refused specifically for `recording_format` | `protocol/categories/settings.py` |
| `0x01` (param `0x09`) | Recording format (fps/sensor-fps/width/height/flags, int16 ×5) — VERIFIED on `POCKET_6K_G2 v7.9` (2026-07-20: `CameraSession.set_recording_format()` real-hardware write+echo cycle with the `0x82` write byte accepted); the camera's own reports still use data-type byte `0x02`. Same category/parameter and payload shape independently confirmed on `POCKET_6K_PRO v8.6` too (still CANDIDATE there); on that camera this write never confirms a resolution retarget to 4K DCI while ProRes is the active codec, 2/2 real `CameraSession` round trips (2026-07-22, `docs/settings.md` §16) — not a timing artifact, but also not a proven camera-side refusal: a passive capture of the camera reaching that exact state through its own body menu (§16 addendum) shows it genuinely holds and reports ProRes/4K DCI, so the gap is in this codebase's write path, not the camera. With the `dimension_enum` search exhausted, `tools/control/send_settings_command.py --data-type INT16` (added 2026-07-23) let this write be retried with the camera's own report byte (`0x02`) instead of the claimed write byte `0x82`, without touching the profile — and ruled that out too (2026-07-23/24): zero fresh confirming reports over a full 8s window, the same signature already established for `0x82`. `video_format`'s unexplained trailing elements were then probed via `--video-format-extra` too (2026-07-24) with no support found either. An `--operation OFFSET` retry with the same absolute target payload (2026-07-24) also got zero response over a 10s window, but per `docs/protocol.md` §4's OFFSET semantics an absolute payload isn't a faithful test — `--raw-payload` (2026-07-24) then sent a genuine `+256` width delta (UHD → 4K DCI) via `OFFSET` instead of the absolute width `4096`, and got the same zero-response signature anyway, ruling the delta hypothesis out too. A follow-up isolating test then sent an equivalent `OFFSET` delta against `codec_quality` instead (2026-07-24) — a family whose `ASSIGN` echo behavior is already well-characterized — and it stayed silent too, pointing at `Operation.OFFSET` being unimplemented camera-wide rather than refused specifically for this parameter. A follow-up test then retried the retarget with the *exact* `fps_int=24` the camera itself reports at that state (every earlier write had used `25`) — TX-confirmed byte-identical to the passive capture, sent from two genuinely different starting states 2/2, with the operator directly confirming the on-screen display never changed either time (2026-07-24) — ruling out both "wrong value" and "echo-only confirmation problem": the write is genuinely ignored, not just unconfirmed. A follow-up test then retried the retarget with the exact camera-reported codec *variant* too (ProRes HQ, not `422`/`PXY` as every earlier attempt used) — this time with the ProRes/HQ/UHD/24fps precondition confirmed by genuine fresh wire echoes immediately beforehand, not just requested (2026-07-24) — still zero response, still no on-screen change. With resolution, fps, codec, variant, data type, and operation now all tried at their confirmed-correct values, **the write-value hypothesis space is exhausted**; the remaining question is whether this transition is reachable over BLE `OUTGOING_CONTROL` at all, not what this codebase sends. **Accepted and guarded, 2026-07-24**: `resolutions."4K DCI".known_unreachable.ProRes` records the finding and `CameraSession.set_camera_format` now raises `BMDUnsupportedError` immediately for this combination instead of attempting a write known to fail (design principle 7) — see `docs/settings.md` §3/§4/§16 for the full write-up | `protocol/categories/settings.py` |
| `0x09` (param `0x01`) | Storage write-margin signal — CANDIDATE, not confirmed causation, see `docs/recording.md` (category `0x09` is the same ambient-telemetry category `TIMECODE` param `0x04` already lives in) | `protocol/categories/storage.py` |

### Data types (`protocol/types.py`)

The coding follows the official *Blackmagic Camera Control Developer
Information* document:

| Value | Type | Notes |
|---|---|---|
| 0 | void / boolean | void = no payload (trigger); boolean = 1 byte per element (0 = false). `DataType.BOOL` is an alias of `DataType.VOID` |
| 1 | int8 | signed byte |
| 2 | int16 | |
| 3 | int32 | |
| 4 | int64 | |
| 5 | string | UTF-8 |
| 128 | fixed16 | signed 5.11 fixed point: `encoded = round(real × 2048)` |
| 130 (`0x82`) | int16 array | NOT official coding — CANDIDATE wire byte reported on the `POCKET_6K_G2 v7.9` recording-format packet (five LE int16 elements), see `docs/settings.md` §3 |

Provenance: data-type bytes sniffer-verified over BLE so far: `0x01` (int8 —
recording command/echo, codec reports), `0x02` (int16 — recording-format and
category-9 reports; note the camera reports the recording-format parameter
with `0x02` even though the claimed *write* byte is `0x82`), `0x03` (int32 —
a shutter-angle report) — all on `POCKET_6K_G2 v7.9` — plus, from the
2026-07-27 photo-capture connect bursts on both cameras: `0x00` (both
flavors — payloadless void reports and a one-byte boolean report), `0x05`
(UTF-8 lens strings), and `0x80` (fixed16 — an aperture report decoding to
exactly the lens's stated f-stop, though with an unexplained second, zero
element). Only int64 (`0x04`) has never been observed on hardware — capture
one before trusting a multi-byte decode. Full discussion:
`docs/protocol.md` §3.

---

## Payload JSON Structure

`payloads/models/<MODEL_KEY>_<FIRMWARE>.json` — validated against `payloads/schema.json` at load time (see `docs/payload_profiles.md` for the full design and rationale):

```json
{
  "_meta": {
    "model": "Pocket Cinema Camera 6K G2",
    "model_key": "POCKET_6K_G2",
    "firmware": "v7.9",
    "ble_name": "A:AF3DC814",
    "status": "VERIFIED | UNVERIFIED"
  },
  "ble": {
    "incoming_property": "indicate",
    "_comment": "Add characteristic_incoming only if overriding the default UUID in constants.py"
  },
  "gap_meta_data": { "readable": false },
  "device_info_meta_data": { "readable": true },
  "commands": {
    "recording": {
      "category": 10,
      "parameter": 1,
      "data_type": "INT8",
      "reserved": 1,
      "values": { "start": 2, "stop": 0 },
      "echo_operation": 2,
      "provenance": {
        "status": "VERIFIED | UNVERIFIED | CANDIDATE",
        "method": "how the values were obtained",
        "capture_refs": ["tools/captures/..."],
        "verified_on": "YYYY-MM-DD",
        "notes": "..."
      }
    }
  },
  "capabilities":   { "supports_raw": true, "supports_photo": true, "supports_playback": true },
  "codecs":         { "BRAW": { "id": 3, "variants": { "Q0": 0, "5:1": 3 } } },
  "resolutions":    { "4K DCI": { "width": 4096, "height": 2160,
                                  "codecs": ["BRAW", "ProRes"],
                                  "dimension_enums": { "BRAW": 8 } } },
  "fps_modes":      { "23.98": { "fps_int": 24, "m_rate": 1, "frame_flags": 19 } }
}
```

Every sniffer-confirmed command family gets one block under `commands`, all the same shape: protocol coordinates, a named `values` map, the observed `echo_operation`, and structured `provenance` (per-command verification state — `_meta.status` still describes the profile as a whole). `values` is optional for multi-element families (codec_quality, video_format, recording_format) whose payloads are composed from the `codecs`/`resolutions`/`fps_modes` lookup tables instead — those tables' provenance rides with the command blocks that consume them (see `docs/settings.md` and `docs/payload_profiles.md`) — and for a `VOID` trigger family, which has no payload at all. `capabilities` is reserved in the schema but only populated once sniffed on that camera. Code reads commands via `profile.require_command(name, value_names)` → `CommandSpec`, and the tables via `require_codec` / `require_resolution` / `require_fps_mode`.

All protocol values come from sniffer captures. `status` is set to `"VERIFIED"` only after testing on real hardware.

---

## Storage Media Monitoring *(planned)*

This section is design intent — no storage monitoring is implemented yet
(`StorageState`/`CameraState` do not exist; see design principle 4).

Storage state is read on connect and updated from `CAMERA_STATUS` notifications. It is tracked in `StorageState` (part of `CameraState`) and covers:

- Slot presence (card inserted or not)
- Card status (ready, formatting, write-protected, error)
- Remaining recording time (derived from free space + current codec/quality/FPS)
- Remaining photo capacity

### How storage state gates operations

| Operation | Pre-condition check | Post-condition check |
|---|---|---|
| Record start | Card ready, remaining time > 0 | `CameraState.is_recording` becomes `True` |
| Record stop | Camera is recording | `CameraState.is_recording` becomes `False`, remaining time decreased |
| Photo capture | Card ready, remaining photos > 0 | Remaining photo count decreased |
| Playback | Card ready, media index readable | Playback state transitions to playing |

If the pre-condition fails, raise `BMDStorageError` immediately — do not attempt the command.

Storage characteristics to monitor are per-camera. Add them to the payload JSON under `storage` as they are confirmed via sniffer.

---

## Logging Conventions

All loggers use `logging.getLogger(__name__)`, or a per-instance child of it
derived from `__name__` (e.g. `logging.getLogger(f"{__name__}.{profile.model_key}")`,
as `camera_controller.py` does for per-session file logging — see
`docs/event_subscription_and_logging.md`). Never invent a logger name that is
not rooted in `__name__`.

### Log levels

| Level | When to use |
|---|---|
| `DEBUG` | Raw BLE bytes (TX and RX), characteristic UUIDs being read, internal state transitions |
| `INFO` | Operation boundaries (connect, disconnect, command sent, verification passed) |
| `WARNING` | Best-effort read failures, unverified profile loaded, CAMERA_STATUS unreliable |
| `ERROR` | Verification failures, storage pre-condition failures, unexpected disconnects |

### Format

Every log line that involves a camera operation must include the camera identity. The transport layer prefixes with `[<ble_name> @ <address>]` (before a BLE address is known — e.g. at profile load — `[<model_key> @ <ble_name>]` is used instead). Example lines, using the real sniffer-verified `POCKET_6K_G2 v7.9` record-start bytes:

```
[A:AF3DC814 @ AA:BB:CC:DD:EE:01] Recording start — sending command
[A:AF3DC814 @ AA:BB:CC:DD:EE:01] TX: FF 05 00 01 0A 01 01 00 02
[A:AF3DC814 @ AA:BB:CC:DD:EE:01] RX: FF 0A ...  (14-byte CAMERA_REPORT echo; payload 02 00 40 00 01 03)
[A:AF3DC814 @ AA:BB:CC:DD:EE:01] Recording start verified via INCOMING_CONTROL echo
```

The `CameraSession` (or `CameraController`) must inject the identity prefix so that every log line from any module is unambiguously tied to the camera that produced it.

### BLE byte logging

Log raw bytes as uppercase hex pairs separated by spaces — matching the format used by Wireshark and nRF Sniffer. This allows direct copy-paste comparison with sniffer captures:

```python
logger.debug("TX: %s", " ".join(f"{b:02X}" for b in packet))
logger.debug("RX echo: %s", " ".join(f"{b:02X}" for b in response))
```

Log TX bytes immediately before writing to `OUTGOING_CONTROL`. Log RX bytes immediately when they arrive on `INCOMING_CONTROL`, before any decoding.

---

## Verification Strategy

For every write command:

1. Confirm `INCOMING_CONTROL` notifications are active and `NotificationRouter` is buffering
2. Write command bytes to `OUTGOING_CONTROL`
3. Await matching echo on `INCOMING_CONTROL` with configurable timeout (default 3 s — bumped from an initial 2 s after real-hardware logs showed occasional echo arrivals taking close to that long)
4. If echo arrives — optionally read `CAMERA_STATUS` as a cross-check
5. If echo times out and camera is still connected — attempt `CAMERA_STATUS` read
6. If neither check confirms the expected state → raise `BMDVerificationError`

The echo must be buffered *before* the write is issued. A router that only starts listening after the write will race against the camera's response.

**Known risk on `POCKET_6K_PRO v8.6` (with a lens attached), unconfirmed on the G2:** a lens-metadata burst (category `0x0C`, params `0x00`–`0x0F` — lens name, aperture, focal length, a focus-distance readout) has repeatedly dominated capture windows during settings work, delaying a genuine settings echo past the window/timeout it belongs to (see `docs/settings.md` §15 for a worked example — a `recording_format` echo arrived in the *next* send's capture window instead of its own). Since this burst competes for the same BLE indication queue a real `CameraSession` write's echo would, it could plausibly delay that echo past the default `echo_timeout_s` (3 s) too, in production, not just in `tools/control/send_settings_command.py`'s fixed-window tooling — a real write could raise `BMDVerificationError` from an echo that was only late, not missing. **Confirmed 2026-07-22** against a real `CameraSession.set_camera_format()` round trip on the PRO (not yet added to `KNOWN_PROFILES`, tested via a locally-edited `examples/change_codec.py`): a `set_video_format` write that genuinely succeeded raised `BMDVerificationError` under the default 3 s timeout because its echo arrived ~4.2 s late, exactly this pattern. Raising `echo_timeout_s` to 6.0 avoided that false negative — but then exposed a second, unrelated, genuine limitation underneath it (`set_recording_format` cannot retarget resolution to 4K DCI while ProRes is active on this camera — see `docs/settings.md` §16). Lesson for any model: a wider timeout can retire the *timing* hypothesis while leaving a real failure exposed underneath — don't stop investigating just because a longer timeout stops the error.

---

## Workflow: Adding Support for a New Camera (Reverse-Engineering Procedure)

This is the concrete, tool-by-tool procedure for bringing up a new `(MODEL_KEY, FIRMWARE)`
pair — which tool to run in which order, and what profile change each step produces.
Follow the phases in order; each one depends on the profile state the previous phase left
behind. Derived from reverse-engineering `POCKET_6K_G2 v7.9` (all phases) and
`POCKET_6K_PRO v8.6` (Phase 2 done, Phase 3 in progress — resolutions, dimension_enums,
and codec ids transcribed, but nothing yet promoted past CANDIDATE) — see
`docs/settings.md` and `docs/command_discovery.md` for the full evidentiary write-ups
behind Phase 3 and Phase 2 respectively.

Two flag conventions to keep straight: `tools/query/`, `tools/sniffers/`, and
`tools/control/` scripts all take `--model-key`/`--firmware` CLI flags. `examples/*.py`
scripts do **not** — they hardcode `MODEL_KEY`/`FIRMWARE` as module-level constants near
the top of the file (edit that constant, or uncomment the alternate model's line where
one is already present, to point at a different camera).

### Phase 1 — Profile scaffold and transport sanity

1. **Scaffold the profile.** Add `payloads/models/<MODEL_KEY>_<FIRMWARE>.json` with only
   `_meta` (`model`, `model_key`, `firmware`, `ble_name` — the real advertised name, never
   a placeholder — `status: "UNVERIFIED"`) and `ble` populated. No `commands` yet.
   Validate against `payloads/schema.json`.
2. **Confirm discoverability** — `python examples/scan_camera.py` (after setting
   `MODEL_KEY`/`FIRMWARE`). Confirms the camera actually advertises under `ble_name`.
3. **Confirm a bare connect works** — `python examples/connect_to_camera.py`. Connect,
   hold, disconnect — nothing else.
4. **Confirm GATT UUIDs match expectations** —
   `python tools/query/ble_services_chars.py --model-key <MODEL_KEY> --firmware <FIRMWARE>`.
   If a characteristic UUID differs from `constants.py`'s default, override it under the
   profile's `ble` section (e.g. `characteristic_incoming`) — never edit `constants.py`
   for a per-model value (design principle 1).
5. **Check GAP metadata readability** —
   `python tools/query/gap_meta_data.py --model-key <MODEL_KEY> --firmware <FIRMWARE>`.
   Record the result in `gap_meta_data.readable`. Known hazard: on `POCKET_6K_G2 v7.9`,
   reading GAP characteristics disconnects the camera — if the new model does the same,
   set `readable: false` and don't retry the read anywhere else for this camera.
6. **Check device-info metadata readability** —
   `python tools/query/device_meta_data.py --model-key <MODEL_KEY> --firmware <FIRMWARE>`.
   Record the result in `device_info_meta_data.readable`.
7. **Confirm notifications stream** — `python examples/monitor_incoming.py` (after
   setting `MODEL_KEY`/`FIRMWARE`). Watch raw INCOMING_CONTROL bytes while performing a
   few obvious actions on the camera body — gives a first feel for the protocol before
   any targeted sniffing begins.

### Phase 2 — Recording start/stop

8. Sniff → discover → paste → verify → session round-trip:
   1. `python tools/sniffers/sniffer_recording.py --model-key <MODEL_KEY> --firmware <FIRMWARE>`
      — passive capture while the operator starts/stops recording on the camera body.
   2. `python tools/control/discover_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE> --label recording --from-capture <path to the capture saved by step 8.1> --values 2,0 --reserved 1,0 --outcomes start,stop`
      — seeds candidates from the passive capture, sweeps them with operator confirmation
      per candidate, emits the ready-to-paste `commands.recording` block (see
      `docs/command_discovery.md`). `--reserved 1,0` tries the G2's known reserved byte
      first; a genuinely new family may need different candidates.
   3. Paste the emitted block into the profile's `commands.recording`, then
      `pytest tests/unit` — no Python code should need to change; the protocol layer
      already handles the recording category generically.
   4. `python tools/control/send_record_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE>`
      — deterministic active send-then-capture, confirming the pasted block's echo on
      demand rather than waiting to catch it inside a passive window.
   5. `python examples/record_start_stop.py` (after setting `MODEL_KEY`/`FIRMWARE`) — the
      real `CameraSession.record_start()`/`record_stop()` round trip with echo
      verification. Passing this promotes `commands.recording.provenance.status` to
      `"VERIFIED"`.

### Phase 3 — Settings: codec, quality, resolution, FPS

9. **Sniff all three families passively**, one capture window per concrete setting so
   each result is unambiguously attributable:
   ```
   python tools/sniffers/sniffer_settings.py --model-key <MODEL_KEY> --firmware <FIRMWARE> \
       --actions res_HD,res_UHD,res_4K_DCI,codec_prores,codec_braw,quality_variant_change,fps_change
   ```
   (adjust the action list to the model's actual resolutions/codecs). The operator
   performs each change on the camera body between prompts. This is expected to confirm
   `codec_quality` (`0x0A/0x00`) and `recording_format` (`0x01/0x09`) reports — on the G2,
   `video_format`'s own channel (`0x01/0x00`) never reported passively at all, so its
   `dimension_enum` values could not be captured this way; assume the same here unless
   proven otherwise, and probe them actively next.
10. **Probe every (resolution, codec) `dimension_enum` actively**, one candidate at a
    time:
    ```
    python tools/control/send_settings_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE> \
        --packet video_format --fps <fps> --dimension-enum 0x<candidate>
    ```
    Typed-yes gated; the operator watches the body, and the resulting `0x01/0x09` report's
    width/height plus the on-screen codec identifies what the enum selects. Repeat per
    candidate until every needed (resolution, codec) pair has a confirmed enum, or the
    search is exhausted for a pair like the G2's 4K DCI/ProRes (see `docs/settings.md`
    §7-§9 for that precedent and the two-step `set_camera_format` proxy workaround it
    needs when a gap can't be closed). For an **exhaustive** sweep across many untried
    candidates in one connected session — e.g. hunting a still-missing enum like
    ProRes/4K DCI — use `tools/control/sweep_dimension_enum.py` instead of repeating
    single-candidate sends by hand: it decodes each result from the wire automatically
    (`--target-resolution`/`--target-codec`) rather than requiring the operator to read
    the on-screen display, which is known-unreliable on at least one camera (see below).
    See `docs/active_camera_control.md` for the full writeup.
    **The G2's proxy workaround is not guaranteed to generalize:**
    on `POCKET_6K_PRO v8.6`, the equivalent proxy workaround's second step
    (`set_recording_format` retargeting resolution within the proxied-to codec) never
    confirms for ProRes/4K DCI, confirmed 2/2 via real `CameraSession` round trips —
    not a G2 pattern to assume elsewhere (see `docs/settings.md` §16). A passive
    capture of the camera reaching the same state through its own body menu then
    confirmed the target state itself is real and correctly held/reported by the
    camera (§16 addendum) — so this is a gap in the write path this codebase uses,
    not proof the camera-side combination is unsupported. Don't assume a retarget
    failure like this means "unreachable" without checking whether the camera can
    be observed in that state at all.
11. **Transcribe confirmed values into the profile** — `commands.codec_quality` /
    `commands.video_format` / `commands.recording_format`, plus the `codecs` /
    `resolutions` / `fps_modes` lookup tables. Nothing may be copied from another model's
    profile (design principle 6); every value must come from this model's own capture.
12. **Confirm each family's write+echo cycle**, one deterministic active send per family:
    ```
    python tools/control/send_settings_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE> \
        --packet codec_quality --codec <codec> --variant <variant>
    python tools/control/send_settings_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE> \
        --packet video_format --resolution <resolution> --codec <codec> --fps <fps>
    python tools/control/send_settings_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE> \
        --packet recording_format --resolution <resolution> --fps <fps>
    ```
    Add `--repeat 2` to any of these to also probe that family's redundant-write echo
    behavior (send the identical command twice; compare whether the second window shows
    `(none observed)`) before relying on a `last_known_*` no-op guard for it in
    `session.py` — every family went silent on a repeated identical write on the G2
    (`docs/settings.md` §11, §14), but that must be reconfirmed per model, not assumed.
13. **Session round-trip** — `python examples/change_codec.py` (after setting
    `MODEL_KEY`/`FIRMWARE`). Runs `CameraSession.set_camera_format()`, which orchestrates
    all three writes with echo verification. Passing this promotes all three families'
    `provenance.status` to `"VERIFIED"`.

    This confirms one combination. Before trusting the profile's full `codecs`/
    `resolutions`/`fps_modes` tables, run `tools/control/sweep_camera_format.py
    --model-key <MODEL_KEY> --firmware <FIRMWARE> --dry-run` first to see the full
    combination count, then a real (possibly narrowed) sweep — it runs
    `set_camera_format()` across every combination the tables claim is supported and
    flags any that never confirm, the same shape of gap `POCKET_6K_PRO v8.6`'s
    ProRes/4K DCI combination turned out to be (`docs/settings.md` §16) after being
    found by accident rather than checked systematically. An `unconfirmed` result is a
    candidate for a `known_unreachable` entry, not an automatic one — it still needs
    the same real-hardware follow-up that investigation took (see
    `docs/active_camera_control.md`) before being written into the profile.

### Phase 4 — Finish

14. Add the `(MODEL_KEY, FIRMWARE)` tuple to `KNOWN_PROFILES` in `camera_profile.py` —
    only after the profile JSON exists (never before — see "What Not To Do").
15. Run `pytest tests/unit` and `ruff check . && ruff format --check .` — both must pass
    before committing.
16. Update the camera registry table at the top of this file (status column, notes).

Every profile JSON change in this procedure needs a doc touch in the same commit, per the
"Feature doc convention": `docs/recording.md` for Phase 2, `docs/settings.md` for Phase 3.

---

## Workflow: Adding a New Command

1. Capture the command via sniffer on the target camera/firmware
2. Add protocol values (category, param, data type, payload encodings) to the profile JSON
3. Add encoder function in `protocol/categories/<category>.py`
4. Define the expected echo or `CAMERA_STATUS` mask for verification
5. Expose the command through `session.py`
6. Write unit test with a mocked BLE client — covers encode, send, echo, verify
7. Test on real hardware before marking profile `VERIFIED`

---

## Testing

| Suite | Location | Hardware | Runs in CI |
|---|---|---|---|
| Unit | `tests/unit/` | No — full mocking | Yes |
| Integration *(planned)* | `tests/integration/` | No — mocked BLE, real round-trips | Yes, once it exists |
| Hardware | Manual | Yes | No |

CI runs on Windows only, Python 3.11 and 3.12, via GitHub Actions. Unit tests must pass on every push. Integration tests must pass before any profile is marked `VERIFIED`.

### Code quality gates — required after every change

After making any code change, Claude must run both of these and fix all failures before committing:

```
python -m pytest tests/unit/
python -m ruff check . && python -m ruff format --check .
```

- If unit tests fail, diagnose and fix the root cause — do not skip or mock away failures.
- If ruff reports lint errors, fix them in the same commit as the code change.
- If ruff reports formatting violations, run `python -m ruff format .` and include the formatted files in the commit.

---

## What Not To Do

- Never hardcode a codec ID, quality ID, FPS encoding, or category/param pair in Python code
- Never copy protocol values from one camera profile to another without sniffing that model
- Never mark a profile `VERIFIED` without testing on real hardware
- Never assume a BLE write succeeded — always verify
- Never update `CameraState` from a sent command — only from received notifications
- Never import `camera_controller`, `notification_router`, or `protocol/` directly in scripts — use `session.py`
- Never catch `Exception` broadly — catch specific BLE and OS exceptions only. Sole carve-out: `contextlib.suppress(Exception)` is permitted for best-effort teardown during disconnect cleanup, where any late WinRT error is unactionable (see `camera_controller.py`)
- Never add a new camera to `KNOWN_PROFILES` before its JSON payload exists
- Never start recording or photo capture without first checking storage state
- Never poll storage state in a loop — read once on connect, update from notifications
- Never log raw BLE bytes as plain integers or Python `repr` — use uppercase hex pairs to match sniffer output
- Never omit the camera identity prefix from operation log lines
