# Sniffer Capture Engine

**Status:** implemented — `tools/common/capture.py` drives both passive sniffers and active control tools.

## Overview

`tools/common/capture.py` is a reusable BLE-notification capture engine
shared by `tools/sniffers/sniffer_<feature>.py` scripts (passive, listen-only)
and `tools/control/*.py` scripts (active — sends a command, then listens;
see `docs/active_camera_control.md`). It exists to serve CLAUDE.md's
"sniffer-first" design principle: every category, parameter, and data type
value used in this codebase must come from a real capture on real hardware,
never be invented. This module is how that capture happens — it has no
knowledge of any specific feature (recording, settings, media, ...); feature
scripts supply only their own list of action labels (and, for the active
mode, the raw command bytes).

`tools/sniffers/sniffer_recording.py` is the first passive consumer — it
captures `record_start` and `record_stop`. `tools/control/send_record_command.py`
is the first active consumer.

---

## Module contents (`tools/common/capture.py`)

| Name | Purpose |
|---|---|
| `DecodedNotification` | Normalized view of one BLE notification: characteristic name/UUID, raw hex, decoded category/parameter/data_type/operation/payload if it parsed as a BMD command packet, or `decode_error` if it didn't. |
| `CaptureWindow` | One labeled action window and the notifications observed during it. |
| `CaptureSession` | All windows captured in one run, plus `active` — the window currently receiving notifications. |
| `decode_notification(characteristic, data)` | Pure function decoding one raw notification. Never raises. |
| `dedupe_triples(notifications)` | `(characteristic_name, category, parameter)` triples, first-seen order, no duplicates. |
| `make_capture_callback(session)` | Builds the Bleak-style `callback(characteristic, data)` that records into whichever window is currently active. |
| `run_capture_windows(cam, labels)` | Drives the interactive (passive) capture: one window per label, operator-triggered. |
| `run_send_and_capture(cam, actions, listen_seconds=3.0)` | Drives the active capture: sends each `(label, command_bytes)` via `cam.write_outgoing_control`, then listens for `listen_seconds`. See `docs/active_camera_control.md`. |
| `print_window_summary(window)` | Console report: deduped triples, then the full raw notification list. |
| `save_capture(model_key, firmware, session)` | Writes the full-fidelity capture to JSON. |
| `configure_console_logging(level=logging.INFO)` | Console logging setup that keeps `BMDCameraController`'s default per-notification DEBUG logging out of the console (see below). |

Both capture modes share the same subscribe step (`_subscribe_capture_callback`),
decode/dedupe/report/save machinery — only how a window's "hot" period starts
and ends differs (operator-triggered vs. self-triggered-then-timed).

---

## Interactive capture-window mechanism

Each window works like this:

1. Prompt: "Get ready, then press Enter to start capturing..."
2. Mark the window `active`.
3. Prompt: "Trigger the action now. Press Enter when done..."
4. Clear `active`.
5. Print the window's summary.

The prompts use `input()`, which blocks a thread. Since BLE notifications
must keep arriving on the asyncio event loop while the operator is reading
the prompt and triggering the action, `input()` is run via
`loop.run_in_executor(None, input, prompt)` rather than awaited directly —
this offloads the blocking call to a worker thread and lets the event loop
keep dispatching Bleak's notification callbacks in the meantime.

`CaptureSession.active: CaptureWindow | None` is a single mutable field that
`run_capture_windows` reassigns per window, rather than using `nonlocal`
inside a closure. The capture callback (`make_capture_callback`) only ever
reads `session.active` at call time and appends to whatever window is
currently "hot". This is safe without any locking because Bleak's
notification callbacks and the coroutine driving the prompts both run on the
same event-loop thread — there is no concurrent mutation to guard against,
only sequencing, which `active`'s reassignment already provides.

---

## What is subscribed, and why TIMECODE is excluded

`run_capture_windows` subscribes the shared callback to `CHARACTERISTIC_INCOMING`
and `CHARACTERISTIC_CAM_STATUS` only. `CHARACTERISTIC_TIMECODE` is
deliberately **not** subscribed here: it ticks roughly once a second
regardless of any triggered action, so it would flood every capture window
with irrelevant timecode notifications and defeat the point of finding what
*changed because of the action*. INCOMING_CONTROL and CAMERA_STATUS are also
exactly the two characteristics CLAUDE.md's verification strategy checks
(echo primary, status secondary), so limiting capture to these two keeps the
sniffer's output directly usable for that later verification wiring.

---

## Keeping the console quiet during prompts: `configure_console_logging`

Even with TIMECODE excluded from the capture buffer, `BMDCameraController`
still installs a default `_log_timecode` handler on connect (via
`subscribe_all()`) that logs every TIMECODE notification — about once a
second — at `DEBUG`. Each controller instance pins its own child logger
(`bmd_ble.camera_controller.<model_key>`) to `DEBUG` in `__init__`, so a
script-level `logging.basicConfig(level=logging.INFO, ...)` does **not**
stop these from reaching the console: `basicConfig`'s `level=` only gates
calls made directly on the root logger, not records already accepted by a
child logger with its own explicit level as they propagate to ancestor
handlers — only each *handler's* own level filters those. Left unfixed, this
floods the console once a second and buries the `input()` prompts entirely.

`configure_console_logging()` fixes this at the right layer: it attaches a
`StreamHandler` with its own `level` (default `INFO`) as the sole console
handler, so DEBUG-level propagated records are filtered out regardless of
which logger produced them. `camera_controller.py`'s own per-instance
`FileHandler` is untouched and keeps writing full DEBUG detail to
`logs/<model_key>_<firmware>/...` — only the console gets quieter. Every
`sniffer_<feature>.py` entrypoint should call this in its `__main__` block
instead of calling `logging.basicConfig` directly.

---

## Connect-burst contamination of early windows

Confirmed on the 2026-07-27 photo-capture runs (`POCKET_6K_G2 v7.9` and
`POCKET_6K_PRO v8.6`, `docs/photo_capture.md` §5.2): after connect, both
cameras drain a large state-report dump over the indication channel at a
throttled ~180ms cadence lasting 10+ seconds. A window opened before the
drain finishes records mid-burst packets that look action-caused — on the
G2 run the burst's ordered `0x0C` lens-string tail landed inside the first
*photo* window, two windows after connect.

Operator rule for every sniffer built on this engine: open the first
capture window only after notifications have slowed to the ~1/s ambient
cadence. Recognition signature when reading a suspect capture after the
fact: ~180ms inter-packet spacing continuing unbroken across a window
boundary, and parameters arriving in ascending order.

`decode_notification` calls `protocol.codec.decode_packet` and catches
`ValueError`. This is expected, not a bug: `CAMERA_STATUS` notifications are
a raw single status byte, not a BMD command packet, so they will always fail
to decode as one. In that case `category`/`parameter`/`data_type`/`operation`/
`payload_hex` are `None` and `decode_error` holds the reason — the raw hex is
still recorded so the notification isn't silently lost.

---

## Triple dedup semantics

A "triple" is `(characteristic_name, category, parameter)`. `dedupe_triples`
collapses repeated identical triples while preserving first-seen order
(`list(dict.fromkeys(...))`). A `CAMERA_STATUS` window with several status
notifications dedupes to a single `("CAMERA_STATUS (Notify)", None, None)`
entry, since none of them decode to a real category/parameter.

---

## Output artifacts

### Console summary

`print_window_summary` prints the deduped triples (category/parameter as
uppercase hex, matching CLAUDE.md's hex logging convention) followed by every
raw notification in the window with its timestamp and hex bytes.

### JSON capture file

`save_capture` writes to:

```
tools/captures/<model_key>_<firmware>/<model_key>_<firmware>_<timestamp>.json
```

mirroring the existing `logs/<model_key>_<firmware>/<model_key>_<firmware>_<timestamp>.log`
convention in `camera_controller.py`. The file contains every notification
per window (full fidelity — `category`/`parameter` as plain JSON integers,
not hex strings, since this feeds populating a profile JSON) plus a
`deduped_triples` convenience array per window.

This directory is not committed — see `.gitignore` — since captures are
per-session, per-hardware raw dumps consumed manually to populate
`payloads/models/<MODEL>_<FW>.json`, not source of truth themselves.

---

## Adding a new sniffer script

A new sniffer (`sniffer_media.py`, `sniffer_metadata.py`, ...) needs only:

1. Its own `ACTION_LABELS` list (e.g. `["photo_capture"]`).
2. The same connect → `run_capture_windows` → `print_window_summary` per
   window → `save_capture` sequence already used in `sniffer_recording.py`.

No part of the interactive loop, decode function, or dedup logic needs to be
copied or modified.

`tools/sniffers/sniffer_settings.py` is the second consumer, built exactly
this way — with one addition: its window labels are overridable via
`--actions`, because settings reverse engineering needs *value-mapping*
sessions (one window per concrete resolution/codec/FPS so each captured
packet is unambiguously attributable to one setting — e.g.
`--actions res_HD,res_UHD,res_4K_DCI` to map another model's width/height
and codec/variant encodings), not just the fixed per-feature labels the
recording sniffer uses. Default labels cover the five settings changes
`docs/settings.md`'s CANDIDATE families describe (codec to ProRes/back,
quality variant, resolution, FPS). Known passive limit (confirmed on the
2026-07-20 G2 run, `docs/settings.md` §5): video_format dimension enums
never appear in notifications and need the active
`send_settings_command.py --dimension-enum` probe instead.

`tools/sniffers/sniffer_photo.py` is the third consumer: same
`--actions`-overridable pattern as the settings sniffer, with one
methodological addition — its default labels include an `idle_baseline`
window (operator does nothing) because photo capture is a single momentary
action with no paired opposite action (unlike `record_start`/`record_stop`),
so without an idle window `tools/common/discovery.py`'s
`seed_triples_from_capture(exclude_ambient=True)` filter would have no
contrast and keep everything. See `docs/photo_capture.md`.

`tools/sniffers/sniffer_sensor_area.py` is the fourth consumer, built the
same way — `idle_baseline` plus one window per concrete setting value
(`sensor_area_2_8k`/`5_7k`/`6k`) — investigating a ProRes-only still-photo
concept the operator reported (docs/photo_capture.md §8) that has no
known BLE representation yet, unlike anything the settings sniffer
already covers. Scaffold only as of this writing; no capture run. See
`docs/photo_capture.md` §10.

---

## Testing

Only `decode_notification` and `dedupe_triples` are unit tested
(`tests/unit/tools/common/test_capture.py`) — they are pure functions with
no BLE, no `input()`, and no filesystem access. `run_capture_windows`,
`run_send_and_capture`, `print_window_summary`, and `save_capture` are
exercised manually against real hardware, matching the existing
`tools/query/*.py` scripts, none of which have unit tests either — CLAUDE.md's
"Hardware" test tier is manual by design.
