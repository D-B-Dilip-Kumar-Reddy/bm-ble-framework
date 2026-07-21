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
| `POCKET_6K_G2` | Pocket Cinema Camera 6K G2 | v7.9 | In progress | Primary reference; most reverse-engineered |
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
| `0x0A` (param `0x00`) | Codec + quality variant — VERIFIED (2026-07-20: `CameraSession.set_codec_quality()` genuine real-hardware write+echo cycle); does NOT switch BRAW↔ProRes (see `docs/settings.md`) | `protocol/categories/settings.py` |
| `0x01` (param `0x00`) | Video format (FORMAT packet) — VERIFIED (2026-07-20: `CameraSession.set_video_format()` 2/2 real-hardware round trip); dimension_enum locks resolution + codec family, the actual BRAW↔ProRes switch. Never appears in notifications itself — enums need active probing (`docs/settings.md` §7–§8) | `protocol/categories/settings.py` |
| `0x01` (param `0x09`) | Recording format (fps/sensor-fps/width/height/flags, int16 ×5) — VERIFIED (2026-07-20: `CameraSession.set_recording_format()` real-hardware write+echo cycle with the `0x82` write byte accepted); the camera's own reports still use data-type byte `0x02` | `protocol/categories/settings.py` |
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

Provenance: data-type bytes sniffer-verified over BLE so far (all on
`POCKET_6K_G2 v7.9`): `0x01` (int8 — recording command/echo, codec reports),
`0x02` (int16 — recording-format and category-9 reports; note the camera
reports the recording-format parameter with `0x02` even though the claimed
*write* byte is `0x82`), and `0x03` (int32 — a shutter-angle report). All
other official codes come from the spec and have not yet been observed on
real hardware — capture one before trusting a multi-byte decode. Full
discussion: `docs/protocol.md` §3.

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

Every sniffer-confirmed command family gets one block under `commands`, all the same shape: protocol coordinates, a named `values` map, the observed `echo_operation`, and structured `provenance` (per-command verification state — `_meta.status` still describes the profile as a whole). `values` is optional for multi-element families (codec_quality, video_format, recording_format) whose payloads are composed from the `codecs`/`resolutions`/`fps_modes` lookup tables instead — those tables' provenance rides with the command blocks that consume them (see `docs/settings.md` and `docs/payload_profiles.md`). `capabilities` is reserved in the schema but only populated once sniffed on that camera. Code reads commands via `profile.require_command(name, value_names)` → `CommandSpec`, and the tables via `require_codec` / `require_resolution` / `require_fps_mode`.

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

---

## Workflow: Adding Support for a New Camera

1. Run `tools/sniffers/` scripts while performing the target action on the camera (or, once a candidate command is known, `tools/control/` scripts to send it directly and capture the response)
2. Analyse captured packets — extract category, parameter, data type, payload bytes. For an unknown command, run `tools/control/discover_command.py`: it seeds from the passive capture, sweeps candidate values with operator confirmation, and emits the ready-to-paste `commands` block (see `docs/command_discovery.md`)
3. Create or update `payloads/models/<MODEL_KEY>_<FIRMWARE>.json` with verified values
4. Run `tools/query/ble_services_chars.py` to confirm UUIDs match expectations
5. Add the tuple to `KNOWN_PROFILES` in `camera_profile.py`
6. Run `pytest tests/unit` — no Python code should need to change if the protocol layer is correct
7. Test on real hardware and set `status` to `"VERIFIED"`
8. Update the camera registry table in this file

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
