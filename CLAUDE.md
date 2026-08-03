# CLAUDE.md — bmd-camera-control

## Project Overview

Python package (`bmd_camera`) for automated Blackmagic Design camera control over Bluetooth Low Energy.

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

Full evidentiary notes for every entry below — the reasoning behind each status,
promotion history, and open gaps — live in `docs/ble/camera_registry.md`. This table
is the at-a-glance summary.

| Model Key | Model Name | Firmware | Status | Notes |
|---|---|---|---|---|
| `POCKET_6K_G2` | Pocket Cinema Camera 6K G2 | v8.6 | In progress | **Primary reference.** All Python defaults point here |
| `POCKET_6K_G2` | Pocket Cinema Camera 6K G2 | v7.9 | Frozen | Former primary reference; hardware upgraded away, no longer testable |
| `POCKET_6K_PRO` | Pocket Cinema Camera 6K Pro | v8.6 | In progress | Second target |
| `URSA_BROADCAST_G2` | URSA Broadcast G2 | v7.5 | Planned | Different category/param combos expected |
| `URSA_MINI_PRO_12K` | URSA Mini Pro 12K | v8.1 | Planned | Different category/param combos expected |
| `POCKET_4K` | Pocket Cinema Camera 4K | v8.6 | Planned | |

Start all new features with `POCKET_6K_G2 v8.6`. Add `POCKET_6K_PRO v8.6` second.

The `ble_name` field in every BLE profile JSON is the real BLE advertisement name
broadcast by the camera — not a placeholder.

---

## Design Principles

These principles describe the **target architecture**. Everything marked
*(planned)* here or in the Package Structure below is design intent that is not
yet implemented — the docs in `docs/` record exactly what exists today. Never
describe a planned subsystem as implemented.

### 1. No hardcoded protocol values
Codec IDs, quality variant IDs, FPS encodings, resolution encodings, category/parameter combinations — none of these belong in code. They live in `payloads/models/<MODEL_KEY>/ble/<FIRMWARE>.json`. Code reads from the profile. The only values permitted in `constants.py` are those that are fixed by the Bluetooth spec or the BMD BLE API spec and do not vary between models.

### 2. Profile-driven behaviour
`CameraProfile` is the single source of truth for all model/firmware-specific constants. Everything the controller and protocol layer need to construct a command comes through the profile.

### 3. Verification-first writes
**Every write command must be verified before reporting success.** Silently assuming a command worked is never acceptable.

Use a dual-check strategy — per transport:
- **BLE**: primary — await an echo on `INCOMING_CONTROL` (fast; subscribe and buffer *before* sending the command, not after); secondary — read `CAMERA_STATUS` as a cross-check
- **REST *(planned)***: primary — a WebSocket `propertyValueChanged` event; secondary — a `GET` readback. `204` on a `PUT` means accepted, not applied

Both checks carry configurable timeouts. If neither confirms the state change, raise `BMDVerificationError`. On `POCKET_6K_G2 v7.9`, `CAMERA_STATUS` notifications are unreliable — always attempt the echo first and treat the status read as a secondary check only.

*Current implementation status:* recording verification is **echo-only** — none of the known `CAMERA_STATUS` bits encode recording state, so there is no meaningful secondary cross-check for it yet. See `docs/ble/session_and_verification.md`.

Photo capture is a harder case still unresolved over BLE: the trigger command is confirmed on real hardware on both cameras, but no BLE channel — neither echo nor `CAMERA_STATUS` — has ever been observed to move in response to it. This is why `CameraSession.capture_photo()` isn't built yet. `POCKET_6K_G2 v8.6`'s USB/HTTP interface is the leading candidate for the out-of-band confirmation channel BLE never had — see `docs/ble/photo_capture.md` §7/§9 for the full evidentiary record and open decision.

### 4. Observable state model *(planned)*
A `CameraState` object reflects the last-known camera state. It is updated **only** from incoming BLE notifications — never inferred from "I sent command X therefore state is now Y". On connect, read the current state before any automation begins. *(The full `state.py` / `CameraState` / `StorageState` object is not yet implemented. A small, notification-driven slice of this already exists directly on `CameraSession` — `is_recording`/`last_stop_reason`, updated only from decoded recording-category notifications, used to detect a camera-initiated stop (e.g. on a slow SD card) without waiting on a command's own echo; `last_known_codec_variant`, updated only from decoded codec_quality-category notifications; and `last_known_recording_format`, updated only from decoded recording_format-category notifications (including `set_video_format`'s own mode-notify confirmations, which share that same category/parameter). All three no-op guards — `set_codec_quality`, `set_video_format`, and `set_recording_format` — use these fields to recognize an already-satisfied write and skip it instead of waiting on an echo the camera won't send for a no-op (real-hardware-confirmed for all three families, 2026-07-21) — see `docs/ble/session_and_verification.md`, `docs/ble/recording.md`, and `docs/ble/settings.md`.)*

### 5. Strict transport / protocol separation
- `camera_controller.py` — BLE transport only: connect, disconnect, raw byte read/write, notification subscription. No BMD protocol knowledge.
- `protocol/` — BMD packet encoding/decoding only. No BLE knowledge.
- `session.py` — composes the two layers. This is the only surface user scripts touch.

Never mix concerns across these boundaries.

### 6. Sniffer-first for all protocol values
Every codec ID, quality variant, FPS encoding, category/parameter pair must originate from a real sniffer capture on that specific camera and firmware. Never copy protocol values from one profile to another without re-verifying on that model. `tools/sniffers/` (passive) and `tools/control/` (active send-then-capture) drive the payload population workflow.

REST's sibling rule: no endpoint is trusted until it has been swept on that exact camera, firmware, and transport (`tools/rest/probe_endpoints.py`). A result from one camera, or over USB, is not evidence about another camera or about LAN/Wi-Fi — see `docs/rest/transport.md`.

### 7. Explicit capability model
Each profile JSON declares what the camera supports (e.g. `supports_raw`, `supports_playback`, `supports_photo`). Code checks capabilities before attempting an operation. Attempting an unsupported operation raises `BMDUnsupportedError` immediately — no silent failures.

A second, distinct flavor of this: `resolutions.<name>.known_unreachable` (codec name → evidence note) records a *software* capability gap — a `(codec, resolution)` combination the camera itself demonstrably supports, but that this codebase's write path cannot reach despite exhausting every write-value hypothesis (real example: `POCKET_6K_PRO v8.6`'s ProRes/4K DCI gap, `docs/ble/settings.md` §16). Never remove the codec from `codecs` on the strength of a `known_unreachable` entry — the camera's own capability is unchanged; only this codebase's current write path is limited. `CameraSession.set_camera_format` checks this before any write and raises `BMDUnsupportedError` immediately, quoting the evidence note. Entries here are added only after a real investigation is exhausted (see `docs/ble/payload_profiles.md`) — `tools/control/sweep_camera_format.py` surfaces *candidates* systematically, but a human reviews the evidence before writing the field.

A third flavor is the opposite kind of fact: `resolutions.<name>.max_fps_int` records a real *camera hardware* ceiling — the camera itself cannot exceed this fps at this resolution at all (real example: `POCKET_6K_PRO v8.6`'s `"6K"` topping out at 50fps, confirmed both by `sweep_camera_format.py`'s first production run and by the operator checking the camera's own UI — `docs/ble/settings.md` §17). Unlike `known_unreachable`, this isn't a software gap to fix later — there's nothing to fix. `CameraSession.set_video_format` and `set_recording_format` both check this before any write (not just the `set_camera_format` orchestration, since both take `(resolution, fps)` directly) and raise `BMDUnsupportedError` immediately for a requested fps above the ceiling. `sweep_camera_format.py` excludes fps values above a resolution's ceiling from its default sweep for the same reason it excludes `known_unreachable` combinations. That same first production run also demonstrated a real methodological hazard worth remembering for any sweep tool: one `unconfirmed` result turned out to be a false negative in the tool's own default echo timeout (confirmed successful on-screen despite reporting failure) — an `unconfirmed` outcome is evidence about that run's timing, not automatically evidence about the camera; check the on-screen state before trusting it as a `known_unreachable`/`max_fps_int` candidate. A same-shape hazard in the opposite direction hit `sweep_dimension_enum.py` on `POCKET_6K_PRO v8.6` (2026-07-27, `docs/ble/photo_capture.md` §10.5): a candidate that looked like a genuine `MATCH` turned out to be a false positive — leftover state from before the write, not a result the candidate caused, exposed only by an immediate repeat run giving a different answer. A `MATCH`, like an `unconfirmed`, is not automatically evidence about the camera either; the tool now tracks and flags this specific failure mode directly.

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
src/bmd_camera/
  __init__.py               # Public API surface — exports CameraSession, CameraProfile,
                            # get_profile, KNOWN_PROFILES, BMDVerificationError,
                            # BMDUnsupportedError
  exceptions.py             # BMDConnectionError, BMDTimeoutError, BMDCommandError,
                            # BMDVerificationError, BMDUnsupportedError, BMDStorageError
                            # (BMDVerificationError and BMDUnsupportedError are raised
                            # today — the latter by the settings writes; the rest are
                            # reserved for the planned subsystems that will use them)
  camera_profile.py         # Load, validate, and cache model/firmware profiles —
                            # shared across transports; loads a model's ble/ profile
                            # always, and its rest/ profile too when one exists
                            # (optional per camera — see rest/ below)
  ble/
    constants.py            # BLE UUIDs and timing constants (fixed by spec)
    scanner.py               # BLE discovery by advertisement name
    camera_controller.py     # BLE transport layer — raw bytes only
    notification_router.py   # Buffer and route INCOMING_CONTROL notifications by (category, param)
    timecode.py               # TIMECODE characteristic decode + clip-duration math
                              # (wrapped BMD packet, distinct characteristic — see docs/ble/timecode.md)
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
                              # POCKET_6K_G2 v7.9 hardware, see docs/ble/settings.md
        media.py              # (planned) Photo capture, playback controls
        metadata.py           # (planned) Video / photo metadata reads
  rest/
    constants.py             # API base path, WS path, default timeouts (fixed by spec)
    client.py                # RestClient — REST transport only, raw status-code handling
                              # (204/501/2xx/other), no camera semantics — see docs/rest/transport.md
    events.py                # RestEventRouter — buffers propertyValueChanged WS events,
                              # mirrors ble/notification_router.py's arm()/wait_for() contract
                              # exactly, keyed by property path instead of (category, parameter)
    exceptions.py            # BMDRestError + re-exports of the shared exception types
    session.py                # (planned) RestCameraSession — user-facing API, Phase 3+
    media.py                  # (planned) Photo-capture confirmation, playback controls
    mapping.py                 # (planned) Codec/resolution/fps name derivation rules

tools/
  common/                   # Shared BLE capture/decode engine (tools/common/capture.py)
                            # and guided-discovery logic (tools/common/discovery.py),
                            # used by both sniffers/ and control/ — not an entrypoint itself
  sniffers/                 # Passive BLE-notification capture for reverse engineering (listen-only)
  control/                  # Active camera control — sends commands, captures the response
                            # (changes real camera state; use deliberately)
  query/                    # Read-only characteristic inspection
  rest/                     # 8.6 REST/WebSocket transport tooling — no BLE, no bmd_camera.ble
                            # imports. probe_endpoints.py (endpoint sweep — read-only by default,
                            # opt-in idempotent write probes; standalone, no bmd_camera imports
                            # at all) and watch_events.py (streams WS events via RestEventRouter,
                            # the first consumer of the Phase 2 library outside its own tests).
                            # See docs/rest/transport.md
  captures/                 # Runtime output of sniffers/, control/, and rest/ scripts (gitignored)

Tools are grouped by folder according to what kind of thing they do — read-only
query, passive listen, or active send — not by feature. Shared library code used
by more than one tool type lives in `tools/common/`, never duplicated per folder
or reached via an awkward cross-folder import. See `docs/ble/active_camera_control.md`.

payloads/
  models/
    <MODEL_KEY>/
      ble/                  # <FIRMWARE>.json — one per firmware, validated against ble_schema.json
      rest/                 # <FIRMWARE>.json — one per firmware, validated against rest_schema.json.
                            # Optional per camera — POCKET_6K_G2/rest/v8.6.json and
                            # POCKET_6K_PRO/rest/v8.6.json are both populated, from
                            # tools/rest/probe_endpoints.py sweep output
  ble_schema.json           # JSON Schema — validates all ble/ payload files at load time
  rest_schema.json          # JSON Schema — validates all rest/ payload files at load time

examples/
  scan_camera.py            # Discover cameras by BLE advertisement name
  connect_to_camera.py      # Connect-only smoke test (connect, hold, disconnect)
  monitor_incoming.py       # Stream raw INCOMING_CONTROL notifications
  record_start_stop.py      # Echo-verified record start/stop via CameraSession
  change_codec.py           # BRAW <-> ProRes round trip via set_camera_format
                            # (codec+quality+resolution+fps orchestration;
                            # see docs/ble/settings.md and docs/ble/session_and_verification.md)
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
| `docs/ble/protocol.md` | Full protocol reference — SDI categories/parameters, data types, operations, BLE GATT layer, spec-vs-sniffer divergences |
| `docs/ble/packet_structure_and_constants.md` | Packet header byte layout, length-field counting base, `protocol/codec.py` design |
| `docs/ble/winrt_ble_connection_hardening.md` | BLE transport reliability on Windows/WinRT — reconnect loop, liveness detection, connection-generation guards |
| `docs/ble/event_subscription_and_logging.md` | Notification subscription strategy, generation-guarding wrapper, per-session file logging |
| `docs/ble/recording.md` | Record start/stop, verification and storage-precondition strategy, per-camera status |
| `docs/ble/sniffer_capture_engine.md` | Reusable BLE-notification capture engine (`tools/common/capture.py`) |
| `docs/ble/active_camera_control.md` | Active camera control — `write_outgoing_control`, `run_send_and_capture`, `tools/control/` tool-type segregation |
| `docs/ble/session_and_verification.md` | `CameraSession`, `NotificationRouter` echo buffering, why `CAMERA_STATUS` isn't a secondary cross-check for recording yet |
| `docs/ble/payload_profiles.md` | Profile JSON structure, `payloads/ble_schema.json` load-time validation, `CommandSpec` API |
| `docs/ble/command_discovery.md` | Guided command discovery (`tools/control/discover_command.py`) |
| `docs/ble/timecode.md` | `TIMECODE` wire format, BCD decode, clip-duration math |
| `docs/ble/settings.md` | Settings families (codec/quality, video format, recording format) — byte layouts, verification runbook |
| `docs/ble/photo_capture.md` | Photo-capture reverse engineering — trigger confirmed on both cameras, no BLE-observable confirmation signal found; Sensor Area BLE investigation (closed, unwritable) |
| `docs/ble/reverse_engineering.md` | Tool-by-tool procedure for bringing up a new `(MODEL_KEY, FIRMWARE)` pair, and for adding a single new command |
| `docs/ble/camera_registry.md` | Full evidentiary notes behind the Camera Registry table above |
| `docs/rest/transport.md` | REST/WebSocket transport (8.6) — addressing the camera over USB, scheme discovery, `tools/rest/probe_endpoints.py`, sweep results for both cameras |

---

## BMD BLE Protocol

Commands are written as binary packets to `OUTGOING_CONTROL`; echoes and responses arrive on `INCOMING_CONTROL`. `docs/ble/protocol.md` is the full reference (packet structure, all SDI categories/parameters, data types, operations, spec-vs-sniffer divergences) and must be read before any protocol work; `docs/ble/packet_structure_and_constants.md` covers the packet header byte layout and `protocol/codec.py` design in detail.

---

## Payload JSON Structure

`payloads/models/<MODEL_KEY>/ble/<FIRMWARE>.json` — validated against `payloads/ble_schema.json` at load time. Every sniffer-confirmed command family gets one block under `commands`: protocol coordinates, a named `values` map, the observed `echo_operation`, and structured `provenance`. See `docs/ble/payload_profiles.md` for the full structure, an example profile, design rationale, and the `CommandSpec` / `require_codec` / `require_resolution` / `require_fps_mode` API.

---

## Storage Media Monitoring *(planned)*

Design intent only — no storage monitoring is implemented yet (`StorageState`/`CameraState` do not exist; see design principle 4). Scope is the SD card slot only; external USB media is out of scope entirely. See `docs/ble/recording.md`'s "Storage Media Monitoring — full design intent" section for the complete pre/post-condition table and scope notes, including how REST's `/media/workingset` may change this plan.

---

## Logging Conventions

All loggers use `logging.getLogger(__name__)`, or a per-instance child of it
derived from `__name__` (e.g. `logging.getLogger(f"{__name__}.{profile.model_key}")`,
as `camera_controller.py` does for per-session file logging — see
`docs/ble/event_subscription_and_logging.md`). Never invent a logger name that is
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

Raw BLE bytes are logged as uppercase hex pairs, never as plain integers or Python `repr` — see `docs/ble/event_subscription_and_logging.md` for the exact convention and code example. REST's own identity-prefix convention (`[<host>]`) will be documented in `docs/rest/transport.md` once a REST client exists.

---

## Verification Strategy

BLE's dual-check strategy — echo primary, `CAMERA_STATUS` secondary, timeouts, buffer-before-write ordering, and the known lens-metadata-burst risk on `POCKET_6K_PRO v8.6` that can delay a genuine echo past its timeout — is fully documented in `docs/ble/session_and_verification.md`. See design principle 3 above for the transport-general statement of this rule.

---

## Workflow: Adding Support for a New Camera / Adding a New Command

The full tool-by-tool reverse-engineering procedure — bringing up a new `(MODEL_KEY, FIRMWARE)` pair phase by phase, the two procedure variants (brand-new model vs. new firmware for an existing model), which camera a script talks to by default, and the checklist for adding a single new command to an already-brought-up camera — has moved to `docs/ble/reverse_engineering.md`. Read it before starting any new camera bring-up or adding a new BLE command.

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
