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
- **REST**: primary — a WebSocket `propertyValueChanged` event; secondary — a `GET` readback. `204` on a `PUT` means accepted, not applied. The property must already be subscribed (via `RestEventRouter.subscribe()`) before a write method can `arm()`/`wait_for()` it — unlike BLE's echo channel, which is always live once `INCOMING_CONTROL` notifications are subscribed globally at connect, each REST property needs its own explicit subscription, and a write method that arms one nothing ever subscribed will silently starve on the primary channel and always fall through to the secondary check. `RestCameraSession.__aenter__` subscribes every property its write methods arm (`/transports/0/record`, `/system/format`) once, at connect time, for exactly this reason — see `docs/rest/session.md`'s "Connection lifecycle"

Both checks carry configurable timeouts. If neither confirms the state change, raise `BMDVerificationError`. On `POCKET_6K_G2 v7.9`, `CAMERA_STATUS` notifications are unreliable — always attempt the echo first and treat the status read as a secondary check only.

*Current implementation status:* BLE recording verification is **echo-only** — none of the known `CAMERA_STATUS` bits encode recording state, so there is no meaningful secondary cross-check for it yet. See `docs/ble/session_and_verification.md`. REST's dual-check is implemented for `RestCameraSession.record_start`/`record_stop` (Phase 4) — event primary, `GET /transports/0/record` readback secondary — and confirmed 6/6 on real hardware (`POCKET_6K_G2` and `POCKET_6K_PRO v8.6`, 2026-08-03), since `tools/rest/probe_endpoints.py` deliberately never exercises this endpoint itself (it would start/stop a real recording) — this session's own verification was always going to be the only confirmation channel it gets. See `docs/rest/session.md`.

Photo capture is a harder case still: the trigger command is confirmed on real hardware on all three currently-verified profiles (`POCKET_6K_G2 v7.9`, `POCKET_6K_PRO v8.6`, `POCKET_6K_G2 v8.6`), but no BLE channel — neither echo nor `CAMERA_STATUS` — has ever been observed to move in response to it. `CameraSession.capture_photo()` (Phase 6) is built anyway, as a deliberate, loudly-documented exception to this principle: it sends the trigger and nothing else, never claims verification it structurally cannot perform, and never raises `BMDVerificationError`. Real confirmation is REST's job — `rest/media.py` polls the Stills subdirectory's own `mtime` in the mount-root listing for a change, composed with the BLE trigger in `examples/capture_photo.py`. This replaced an earlier filename-guessing design that two real-hardware runs disproved (`POCKET_6K_PRO v8.6`, 2026-08-04: the second run reported "NOT confirmed" for a photo the camera really had taken); a third run confirmed the `mtime`-based redesign works. `rest/media.py` also offers `guess_new_still_path()` — an opt-in, informational-only best-effort filename lookup (never gating the actual confirmation) built on the same evidence, added on request once the redesign was confirmed — see `docs/ble/photo_capture.md` §7/§11 for the full evidentiary record and the reasoning behind the exception, the redesign, and the filename lookup.

### 4. Observable state model *(planned)*
A `CameraState` object reflects the last-known camera state. It is updated **only** from incoming BLE notifications — never inferred from "I sent command X therefore state is now Y". On connect, read the current state before any automation begins. *(The full `state.py` / `CameraState` / `StorageState` object is not yet implemented on the BLE side — the heading tag stays `*(planned)*` for that reason, and none of what follows in this note changes BLE's own design. **Closed on the REST side, Phase 9**: `rest/state.py` now holds a real `CameraState` (`is_recording`, `last_known_storage`, `playback_interrupted`, `last_known_play`/`last_known_stop`, `_in_playback`) and the pre-existing `StorageDevice`/`StorageState` dataclasses, with `RestCameraSession` exposing every field through an identically-named property — no external interface change, see `docs/rest/session.md`'s `is_recording` section and `state.py`'s own module docstring. A small, notification-driven slice of this already exists directly on `CameraSession` — `is_recording`/`last_stop_reason`, updated only from decoded recording-category notifications, used to detect a camera-initiated stop (e.g. on a slow SD card) without waiting on a command's own echo; `last_known_codec_variant`, updated only from decoded codec_quality-category notifications; and `last_known_recording_format`, updated only from decoded recording_format-category notifications (including `set_video_format`'s own mode-notify confirmations, which share that same category/parameter). All three no-op guards — `set_codec_quality`, `set_video_format`, and `set_recording_format` — use these fields to recognize an already-satisfied write and skip it instead of waiting on an echo the camera won't send for a no-op (real-hardware-confirmed for all three families, 2026-07-21) — see `docs/ble/session_and_verification.md`, `docs/ble/recording.md`, and `docs/ble/settings.md`. `RestCameraSession` holds the same discipline on the REST side: its `is_recording` updates only from `/transports/0/record` `propertyValueChanged` events, never from a request the session itself made — see `docs/rest/session.md`. Real-hardware-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-05: the camera's body-menu-only "IF CARD DROPS FRAME" setting (`Alert` / `Stop Recording`, no REST endpoint or subscribable property of any kind — confirmed absent, not merely unswept) triggered `Stop Recording` twice at a demanding `BRAW 3:1`/`6K` combination pushed past 23.98fps, and `wait_while_recording()`'s early-return contract caught both correctly with zero code changes — the REST analogue of this same principle's "camera-initiated stop on a slow SD card" line, now demonstrated rather than theoretical. A third run with the policy switched to `Alert` recorded the same demanding combination cleanly for the full duration with nothing outside the routine event set anywhere in the feed — confirming `Alert` mode is a real, permanent blind spot rather than a gap this codebase happens not to cover yet. See `docs/rest/session.md`'s `is_recording`/`wait_while_recording()` section for the full evidentiary write-up. The same discipline extends to playback: `RestCameraSession.playback_interrupted`/`wait_for_playback_interrupt()` (Phase 8 item 2, part 2) is set only from a `/transports/0/playback` speed deviation or a `/transports/0` mode-left-`"Output"` event, never from a `pause()`/`stop()` call the session itself made — real-hardware-confirmed end to end, `POCKET_6K_G2 v8.6`, 2026-08-05: an out-of-band interrupt (card pulled or stop/pause pressed on the camera body) mid-`play()` was detected in 13.5s, with `last_known_stop` corroborating and the camera still reporting `mode: "Output"` — a speed-only transition, the playback analogue of the recording-side finding above. Four more runs the same day (three self-requested sanity passes plus three more real interrupts at 19.1s/6.2s/3.2s) reconfirmed the detection, including after the trigger was broadened from a fixed `speed == 0` check to `speed != _expected_speed` (any deviation from the speed this session itself last set, not only a drop to zero) — every interrupt across all five runs happened to land on a full stop, so the broadened trigger's own reason for existing (a camera-initiated speed change landing on something other than zero) remains real-hardware-unconfirmed. See `docs/rest/session.md`'s `playback_interrupted` section.)*

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

A third flavor is the opposite kind of fact: `resolutions.<name>.max_fps_int` records a real *camera hardware* ceiling — the camera itself cannot exceed this fps at this resolution at all (real example: `POCKET_6K_PRO v8.6`'s `"6K"` topping out at 50fps, confirmed both by `sweep_camera_format.py`'s first production run and by the operator checking the camera's own UI — `docs/ble/settings.md` §17). Unlike `known_unreachable`, this isn't a software gap to fix later — there's nothing to fix. `CameraSession.set_video_format` and `set_recording_format` both check this before any write (not just the `set_camera_format` orchestration, since both take `(resolution, fps)` directly) and raise `BMDUnsupportedError` immediately for a requested fps above the ceiling. `sweep_camera_format.py` excludes fps values above a resolution's ceiling from its default sweep for the same reason it excludes `known_unreachable` combinations. That same first production run also demonstrated a real methodological hazard worth remembering for any sweep tool: one `unconfirmed` result turned out to be a false negative in the tool's own default echo timeout (confirmed successful on-screen despite reporting failure) — an `unconfirmed` outcome is evidence about that run's timing, not automatically evidence about the camera; check the on-screen state before trusting it as a `known_unreachable`/`max_fps_int` candidate. A same-shape hazard in the opposite direction hit `sweep_dimension_enum.py` on `POCKET_6K_PRO v8.6` (2026-07-27, `docs/ble/photo_capture.md` §10.5): a candidate that looked like a genuine `MATCH` turned out to be a false positive — leftover state from before the write, not a result the candidate caused, exposed only by an immediate repeat run giving a different answer. A `MATCH`, like an `unconfirmed`, is not automatically evidence about the camera either; the tool now tracks and flags this specific failure mode directly. The same shape recurred a third time in `rest/media.py`'s `guess_new_still_path()` (2026-08-04, `docs/ble/photo_capture.md` §11.2): two captures a clock-minute apart both matched the same timestamp candidate, and an ascending-order index search returned the same stale, already-existing filename for both — fixed by always preferring the highest matching index, since `guess_new_still_path()` never gates the real `mtime`-based confirmation and only mislabels which file it thinks was written.

### 8. Fail loud on unverified profiles
If `status == "UNVERIFIED"` in a profile JSON, log a prominent warning at session start. Code can still run against unverified profiles, but the user must know.

### 9. Graceful degradation for reads only
**Reads** (metadata, GAP info, device info) are best-effort — return `None` on failure, do not raise. **Writes** are verification-first — they must confirm success or raise. Write degradation is never silent.

### 10. Storage-aware operations
Before recording or photo capture, verify the storage media is ready (card inserted, not full, not in an error state). After recording or capture, confirm storage state has updated (e.g. remaining space decreased, clip count increased). Storage state is part of `CameraState` and is updated from notifications — never polled in a loop.

If storage state is unknown or unhealthy at operation time, raise `BMDStorageError` before attempting the command. Do not let the camera silently fail to save media.

*Current implementation status:* `RestCameraSession`'s `_require_storage_ready()` implements the pre-flight half of this (a one-time `storage_state()` check before `record_start()`, see design principle 3's REST paragraph — Phase 9 changed its gating field from `remaining_record_time` to `remaining_space`, real-hardware-confirmed not to regress, `POCKET_6K_G2 v8.6`, 2026-08-13). Continuous, notification-driven storage tracking — the "never polled in a loop" half — has its first real implementation as `last_known_storage`/`wait_for_low_storage()` (Phase 8 item 1, `/media/workingset` `propertyValueChanged` events, real-hardware-confirmed live and pushing, `POCKET_6K_G2 v8.6`, 2026-08-05) — see `docs/rest/session.md`. The explicit post-condition half ("remaining space decreased, clip count increased") is `confirm_new_clip()` (Phase 9) — real-hardware-confirmed the same day as the gate change: correctly identified the one new clip and computed its exact byte cost from a real 600s recording.

### 11. Async-first
All I/O is async. No blocking calls anywhere. Use `asyncio.wait_for()` for all timeouts.

---

## Package Structure

Entries marked *(planned)* do not exist yet — they are the target layout for
future subsystems. Everything else is implemented and on disk today.

```
src/bmd_camera/
  __init__.py               # Public API surface — exports CameraSession, RestCameraSession,
                            # CameraProfile, get_profile, KNOWN_PROFILES, BMDVerificationError,
                            # BMDUnsupportedError
  exceptions.py             # BMDConnectionError, BMDTimeoutError, BMDCommandError,
                            # BMDVerificationError, BMDUnsupportedError, BMDStorageError
                            # (raised today: BMDVerificationError by BLE settings/recording
                            # writes and REST record_start/record_stop; BMDUnsupportedError
                            # by BLE settings writes and REST capability checks;
                            # BMDConnectionError by REST transport failures;
                            # BMDStorageError by RestCameraSession.clips()'s no-media
                            # translation and record_start()'s storage precondition.
                            # BMDTimeoutError and BMDCommandError remain unused, reserved
                            # for future subsystems)
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
        media.py              # Photo capture trigger encoding (encode_photo_trigger,
                              # Phase 6) — no echo/decode, none exists on the wire; see
                              # docs/ble/photo_capture.md §11. Playback controls (planned)
        metadata.py           # (planned) Video / photo metadata reads
  rest/
    constants.py             # API base path, WS path, default timeouts (fixed by spec)
    client.py                # RestClient — REST transport only, raw status-code handling
                              # (204/501/2xx/other), no camera semantics — see docs/rest/transport.md
    events.py                # RestEventRouter — buffers propertyValueChanged WS events,
                              # mirrors ble/notification_router.py's arm()/wait_for() contract
                              # exactly, keyed by property path instead of (category, parameter)
    exceptions.py            # BMDRestError + re-exports of the shared exception types
    state.py                  # CameraState — RestCameraSession's notification-driven state
                              # surface (Phase 9, closes design principle 4's *(planned)*
                              # status on the REST side). Holds is_recording,
                              # last_known_storage, playback_interrupted, last_known_play/
                              # last_known_stop, _in_playback — every field session.py used
                              # to hold as plain attributes, moved here unchanged in
                              # behavior. Also home to StorageDevice/StorageState (moved
                              # from session.py, unchanged). RestCameraSession exposes every
                              # field via an identically-named @property delegating to
                              # self.state.<field> (getter+setter for plain data fields;
                              # getter-only for playback_interrupted, a mutable asyncio.Event
                              # mutated via .set()/.clear(), never reassigned) — no external
                              # interface change, confirmed by a full-file review finding
                              # every existing session.py call site is plain
                              # self.attr/self.attr=value syntax, transparent to properties.
                              # BLE's own ble/state.py remains unbuilt and *(planned)* —
                              # this file does not touch ble/session.py's separate
                              # notification-observer pattern
    session.py                # RestCameraSession — user-facing API. Read verbs
                              # (get_format, supported_formats, storage_state, clips,
                              # timecode, clip_timecode, notification-driven is_recording
                              # and last_known_storage (Phase 8 item 1 — /media/workingset
                              # subscribed alongside the other four properties, confirmed
                              # live and pushing real values on real hardware first
                              # (POCKET_6K_G2 v8.6, 2026-08-05, tools/rest/watch_events.py)
                              # before building on it; wait_for_low_storage() mirrors
                              # wait_while_recording()'s shape but explicitly documents the
                              # opposite return-value polarity, given this codebase's own
                              # history with wait_while_recording's inverted-contract bug —
                              # see docs/rest/session.md's last_known_storage/
                              # wait_for_low_storage() section; not yet run against real
                              # hardware itself — tools/rest/verify_low_storage.py is the
                              # real-hardware verification script for this gap specifically —
                              # two runs, POCKET_6K_G2 v8.6, 2026-08-05: the "stays healthy"
                              # branch (False after a full 1800s timeout, card at 117.57GB ->
                              # 55.02GB without crossing 10GB) and the harder "live crossing
                              # mid-wait" branch (True after 3.1s waiting on the first real
                              # /media/workingset push, card already below a 50GB threshold —
                              # last_known_storage was still None at call time so this wasn't
                              # the immediate-return shortcut) both confirmed; only the
                              # shortcut itself (already-populated, already-low
                              # last_known_storage at call time) has no dedicated run yet),
                              # timeline_clip_ids — a plain GET /timelines/0, added to let
                              # a caller inspect the timeline independently of
                              # select_clip()'s own membership poll — closed select_clip()'s
                              # last open question: two runs (opposite directions, two real
                              # format pairs) confirmed POST /timelines/0/add fully replaces
                              # the timeline's contents even though DELETE never runs (501),
                              # no stale cross-format entries left behind); tools/rest/
                              # diagnose_timeline.py's first run (2026-08-05, built for a real
                              # select_clip() failure on a freshly-reformatted 128GB card)
                              # then found POST /timelines/0/add is a no-op whenever the
                              # target clip's format doesn't match the camera's live format —
                              # which puts the "POST fully replaces the timeline" reading
                              # above in question too: every confirmed success, including the
                              # stale-entries one, switched format to match the target clip
                              # first, so none can distinguish the POST doing the work from
                              # the format switch alone already doing it. Closed same day via
                              # diagnose_timeline.py --skip-post: a format switch with zero
                              # DELETE/POST sent populated the timeline on its own — POST
                              # /timelines/0/add has never been shown to do anything on this
                              # firmware (single-matching-clip case only; the multi-clip
                              # seven-clips-share-a-format scenario remains untested with POST
                              # fully absent). See docs/rest/session.md's select_clip()
                              # section, finding #7; writes —
                              # record_start/record_stop (Phase 4, real-hardware-
                              # confirmed; record_stop's secondary GET readback is polled
                              # against a wider dedicated budget, stop_verify_timeout_s
                              # (default verify_timeout_s * 3), instead of fired once —
                              # see docs/rest/session.md's record_start()/record_stop()
                              # section; record_start()'s _require_storage_ready() precondition
                              # now gates on remaining_space alone, not remaining_record_time
                              # (Phase 9 — the latter confirmed stale immediately after a
                              # format switch, docs/rest/session.md's rest_record_test_clip.py
                              # write-up; the former confirmed accurate in every run to date),
                              # and confirm_new_clip()/RecordingResult (Phase 9) formalizes
                              # that same script's manual before/after clips() diff and
                              # remaining_space delta into a reusable method — raises
                              # BMDVerificationError on zero or on more than one new clip
                              # found (never guesses which is "the" recording), and requires
                              # clips_before to come from the same connected session
                              # (clip_unique_id confirmed unstable across reconnects).
                              # Real-hardware-confirmed, POCKET_6K_G2 v8.6, 2026-08-13
                              # (examples/rest_record_test_clip.py, ported): a fourth run
                              # reconfirmed the remaining_record_time staleness finding in the
                              # same shape (61245s unchanged across the format switch, only
                              # updating to 12401s once recording started) while
                              # record_start() succeeded cleanly under the new gate;
                              # confirm_new_clip() correctly identified clip_unique_id=20 (the
                              # only new id) with bytes_written=35390881792 computed exactly —
                              # the first real-hardware confirmation of the positive path.
                              # record_stop did not raise — the first real-hardware run since
                              # stop_verify_timeout_s was added. The zero/multi-new-clip
                              # branches and bytes_written's None-returning cases remain
                              # real-hardware-unexercised. See docs/rest/session.md's
                              # confirm_new_clip() and record_start()/record_stop() sections)
                              # and set_camera_format (Phase 5, dual-check
                              # verified, live-capability-gated via supported_formats(),
                              # optional sensor_resolution param for disambiguating
                              # among several camera-offered pairings — see
                              # tools/rest/sweep_camera_format.py). ProRes/4K DCI
                              # real-hardware-confirmed on both cameras — the exact
                              # combination BLE's write path can't reach. Playback/gallery
                              # writes (Phase 7) — select_clip, enter_playback/
                              # exit_playback, play/pause/stop, shuttle/seek — built,
                              # dual-check verified. /transports/0/playback's real body
                              # ({"type", "loop", "singleClip", "speed", "position"}) is
                              # real-hardware-confirmed (POCKET_6K_PRO v8.6, 2026-08-04);
                              # shuttle()/seek()/_put_playback() read-modify-write that
                              # shape, and seek() takes position (frames). select_clip()
                              # replaces an earlier set_timeline(clip_unique_ids: list[int])
                              # design that the same real-hardware round disproved outright:
                              # requesting one clip while the camera's format matched it
                              # produced a GET /timelines/0 readback of every clip sharing
                              # that format (seven clips, not one) — confirmed two
                              # independent ways (the REST readback, and the camera's own
                              # on-screen playback view showing "CLIP 1/7" for the same
                              # group). The camera has no concept of a caller-curated
                              # playlist, so select_clip(clip_unique_id) picks one clip,
                              # switches format to match it via a new reverse Clip ->
                              # profile-vocabulary mapping (mapping.py's
                              # resolve_ble_codec_name), and confirms the clip is a member
                              # of whatever timeline comes back — not that it's alone. This
                              # exact combination (reverse-map, switch format, sync
                              # timeline) is now real-hardware-confirmed too: the full Phase
                              # 7 sequence (select_clip through exit_playback) ran clean on
                              # both POCKET_6K_G2 and POCKET_6K_PRO v8.6, 2026-08-04, via
                              # examples/rest_playback.py. A later run (POCKET_6K_G2 v8.6,
                              # same day) finally exercised the set_camera_format() switch
                              # branch itself (earlier runs' clips already matched) and
                              # surfaced something unexpected: after exit_playback(), the
                              # camera's format had reverted to what it was before
                              # select_clip() ran, though nothing explicitly requested that.
                              # Three follow-up runs isolated the cause conclusively:
                              # select_clip() alone, and select_clip()+enter_playback()
                              # alone, both left the camera at the switched format with no
                              # revert (ruling out select_clip()/set_camera_format() and
                              # enter_playback()); only removing exit_playback() from the
                              # sequence removed the revert. CONFIRMED: exit_playback()
                              # (PUT /transports/0 {"mode": "InputPreview"}) restores
                              # whatever format preceded entry into Output/playback mode —
                              # stop() triggers the same revert since it's an alias for
                              # exit_playback(). No opt-out exposed; a caller needing a
                              # specific post-playback format must call
                              # set_camera_format() again explicitly — see
                              # docs/rest/session.md for the full four-run trail and
                              # exactly which endpoints/field names are sweep-confirmed vs.
                              # this migration's own plan-derived hypotheses;
                              # playback_interrupted / wait_for_playback_interrupt(timeout)
                              # (Phase 8 item 2, part 2) — the playback analogue of
                              # is_recording/wait_while_recording, set from a
                              # /transports/0/playback speed deviating from _expected_speed
                              # (the speed this session's own last confirmed write actually
                              # set — broadened from a fixed speed==0 check so a
                              # camera-initiated change to any other speed is caught too, not
                              # only a full stop; real-hardware-confirmed as a working trigger,
                              # but every observed interrupt across all 5 real-hardware runs so
                              # far has landed on 0, so the "deviates to nonzero" branch
                              # specifically remains unexercised) or /transports/0
                              # mode-left-"Output", that arrived with none of this session's own
                              # writes in flight — both _playback_write_in_flight and
                              # _transport_mode_write_in_flight guard *both* branches, not just
                              # the one matching their own property, after a real-hardware find
                              # (POCKET_6K_G2 v8.6, 2026-08-05, this tool's own sanity phase):
                              # leaving "Output" mode also pushes a side-effect
                              # PLAYBACK_PROPERTY speed=0 event, which the original per-property
                              # guard didn't cover — fixed same day; see session.py's _on_event
                              # docstring. PLAY_PROPERTY/STOP_PROPERTY (Phase 8 item 2 part 1's
                              # confirmed-real push shape) are subscribed by __aenter__ and
                              # tracked as last_known_play/last_known_stop, a corroborating
                              # signal only — not the trigger, since neither has an ordering
                              # guarantee relative to _put_playback()'s own dual-check the way
                              # PLAYBACK_PROPERTY does. tools/rest/verify_playback_interrupt.py
                              # is the real-hardware verification script; a first run's sanity
                              # phase found and fixed the defect above (fixed the same day),
                              # then a second run (--clip-id 2, sidestepping the first run's
                              # unrelated select_clip() sensor-resolution ambiguity) confirmed
                              # both phases end to end against the (at-the-time) fixed speed==0
                              # trigger: phase 1 clean, and phase 2's real camera-initiated
                              # interrupt detected in 13.5s. Two more runs the same day
                              # (against the fixed==0 check once more, then twice against the
                              # broadened trigger after pulling it) reconfirmed both versions
                              # clean at 19.1s/6.2s/3.2s — see docs/rest/session.md's
                              # playback_interrupted section for the full five-run trail.
                              # device_info(device_name)/doformat_supported_filesystems()
                              # (Phase 10 — read verbs, `DeviceInfo` dataclass) and
                              # format_device(device_name, *, confirm, filesystem,
                              # volume=None, timeout=120.0, poll_interval_s=1.0) — the only
                              # media-erasure capability the official BMD REST spec (11
                              # OpenAPI/AsyncAPI YAML files supplied by the user) documents
                              # at all; no spec file has a per-clip or per-still delete
                              # endpoint, TimelineControl.yaml's DELETE /timelines/0 only
                              # clears the timeline object, never clip files on disk.
                              # format_device() GETs a one-time format key then PUTs it back
                              # with filesystem/volume, gated behind a mandatory
                              # confirm=True (ValueError otherwise, before any request is
                              # sent) since this is the first genuinely irreversible write in
                              # this codebase. Its capability check deliberately gates on
                              # endpoint.supported (GET), not put_supported, unlike every
                              # other write here — .../doformat is in
                              # tools/rest/probe_endpoints.py's NEVER_WRITE list, so
                              # put_supported can structurally never be sweep-confirmed for
                              # it. Verification is the weakest in this codebase by
                              # necessity: Notification.yaml's deviceProperty enum has no
                              # entry for any /media/devices/... path, so there is no WS
                              # event to dual-check against at all — the only signal is
                              # polling device_info(device_name).state, requiring an observed
                              # "Formatting" state before a later terminal state counts as
                              # completion (guards against a stale pre-format "Mounted" read
                              # being mistaken for success). Whether /mounts/... (the separate
                              # filesystem surface Phase 6 already uses for photo
                              # confirmation, outside the 11 spec files entirely) supports
                              # DELETE for individual clip/still files remains an open
                              # question, explicitly deferred at the user's own request —
                              # now under investigation via tools/rest/probe_endpoints.py's
                              # --probe-mounts-delete/--delete-real-file (see that tool's
                              # own Package Structure entry below and docs/rest/transport.md's
                              # Mode 3 section).
                              # filesystem is a required argument, not the optional one
                              # MediaControl.yaml itself describes it as — real-hardware-
                              # confirmed, POCKET_6K_G2 v8.6, 2026-08-13: the first version
                              # left it optional (per the spec) and the camera rejected the
                              # omitted-filesystem PUT with 400 {"error": "Field 'filesystem'
                              # missing from request body."}. A second run, filesystem fixed,
                              # got 400 {"error": "Field 'volume' missing from request
                              # body."} instead — the camera validates one field at a time,
                              # so the first run's "volume wasn't rejected, so it's optional"
                              # reading was wrong; it just hadn't been checked yet. volume
                              # stays str | None = None, but is now resolved for real: when
                              # omitted, format_device() reads the device's current volume
                              # from storage_state() and sends that, raising ValueError
                              # before any request if the device isn't found there or has no
                              # volume of its own. A third run (2026-08-13, both fields
                              # resolved — filesystem="ExFAT", note the real camera's casing
                              # differs from the spec's own "ExFat" example; volume defaulted
                              # from the device's current name) SUCCEEDED end to end: PUT at
                              # 15:37:02.796, device_info("sd0").state observed moving
                              # "Mounted" -> "Formatting" -> "Mounted", completion logged at
                              # 15:37:07.871 — a real 1TB full-card format in ~5s, well inside
                              # the default timeout=120.0/poll_interval_s=1.0. Confirmed by
                              # storage_state(): clip_count 20 -> 0, remaining_space restored
                              # to within ~33MB of total_space. First real-hardware
                              # confirmation of the "Formatting"-must-be-observed-first guard
                              # working on the success path (necessity of the guard — i.e.
                              # rejecting a genuinely stale "Mounted" read — still has no
                              # dedicated real-hardware reproduction, since the first poll
                              # after this PUT already found "Formatting"). See
                              # docs/rest/session.md's Phase 10 section and
                              # examples/rest_format_device.py
                              # delete_clip(clip_unique_id, *, confirm) (Phase 11) —
                              # permanently DELETEs one clip's real /mounts/... path.
                              # Composes the exact sequence confirmed by hand in Postman
                              # after tools/rest/probe_endpoints.py's own
                              # --delete-real-file attempt crashed on a binary-body bug
                              # (see that tool's entry below): GET 200
                              # (application/octet-stream) -> DELETE 200 OK -> GET 404,
                              # POCKET_6K_G2 v8.6, 2026-08-13. confirm has no default,
                              # mirroring format_device()'s exact gate. clip_unique_id
                              # resolved fresh via clips(), ValueError if not found
                              # (matching select_clip()'s discipline). The real
                              # /mounts/... path is built from a single confirmed
                              # sample — file_path's basename placed directly under
                              # resolve_active_mount()'s mount root (rest/media.py),
                              # the internal path's reel subdirectory dropped entirely
                              # — not a general rule; a camera with clips in a real
                              # mount subdirectory would break this, no second data
                              # point exists yet. Verification is RestClient.exists()
                              # before and after DELETE, never get() (a clip's body is
                              # binary — exists() is built to never touch it, the exact
                              # crash class probe_endpoints.py's request() hit). No
                              # polling loop — the confirmed sequence completed
                              # synchronously. Only clip deletion is confirmed; no
                              # delete_still() exists (Stills' own 500 listing defect
                              # means no still path has been independently confirmed
                              # the way this clip's was). RestClient.delete() (client.py)
                              # gained api_prefixed: bool = True for this, mirroring
                              # get()/exists() — it previously had no way to reach
                              # /mounts/... at all. First real-hardware run,
                              # POCKET_6K_G2 v8.6, 2026-08-13 (examples/rest_delete_clip.py,
                              # clip_unique_id=23): the file-level deletion confirmed
                              # exactly as designed (~1.2s, DELETE to confirmed-gone), but
                              # GET /clips/list still listed the clip immediately
                              # afterward in the same session — the file was genuinely
                              # gone (confirmed by the same exists() mechanism Postman
                              # verified), the camera's clip index just didn't reflect it
                              # in the same breath. Not a defect in delete_clip()'s own
                              # verification, which never claimed anything about
                              # /clips/list. Added a best-effort, BMDStorageError-
                              # swallowing, never-raising post-check: logs a WARNING if
                              # clip_unique_id is still listed after confirmed deletion,
                              # purely informational (design principle 9). Whether
                              # /clips/list ever catches up (delay, reconnect, or some
                              # other refresh) is unconfirmed — not investigated further
                              # in this run. See docs/rest/session.md's Phase 11 section.
    mapping.py                 # Codec name derivation between the BLE profile's vocabulary
                              # and REST's own spelling — confirmed strings always win
                              # (design principle 1); this is a fallback seed only.
                              # resolve_ble_codec_name is the reverse direction (REST ->
                              # BLE), table-lookup only with no derivation fallback, used by
                              # select_clip() to turn a Clip's REST-reported codec back into
                              # a (family, variant) set_camera_format() accepts
    timecode.py                # REST TIMECODE decode — BCD HH:MM:SS:FF, big-endian,
                              # byte-reversed relative to BLE; reuses ble/timecode.py's
                              # Timecode dataclass and duration_seconds() as-is. The same
                              # decode_rest_timecode also handles GET /transports/0/timecode's
                              # second field, clip (position within the current clip,
                              # confirmed by the official Notification.yaml spec and real
                              # hardware to share the identical BCD encoding — exposed as
                              # RestCameraSession.clip_timecode()). Neither field can show
                              # a dropped frame — real-hardware-confirmed, POCKET_6K_G2
                              # v8.6, 2026-08-05: both decoded across a full recording
                              # containing an operator-witnessed drop, advanced perfectly
                              # smoothly throughout (zero backwards jumps, zero gaps beyond
                              # normal polling cadence) — both are wall-clock/fps position
                              # counters, not a record of what was actually written. See
                              # docs/rest/session.md's is_recording/wait_while_recording()
                              # section
    media.py                  # Photo-capture confirmation (Phase 6, real-hardware-
                              # confirmed) — resolve_active_mount, stills_marker,
                              # wait_for_new_still; confirms via the Stills subdirectory's
                              # own mtime in the mount-root listing, not a filename guess
                              # (an earlier filename-prediction design was disproven on
                              # real hardware first — see docs/ble/photo_capture.md §11).
                              # guess_new_still_path() is a separate, opt-in, informational-
                              # only best-effort filename lookup that never gates
                              # confirmation. No BLE channel (echo/CAMERA_STATUS) moves on a
                              # photo trigger, on either camera. Playback controls (planned)

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
                            # at all), watch_events.py (streams WS events via RestEventRouter,
                            # the first consumer of the Phase 2 library outside its own tests),
                            # smoke_test_client.py (exercises RestClient's status-code
                            # contract and, opt-in, a full arm()/PUT/wait_for() write-verify
                            # round trip — the two pieces watch_events.py doesn't touch), and
                            # sweep_camera_format.py (Phase 5 — exhaustive codec/quality/
                            # resolution/fps/sensor-area verification sweep via
                            # RestCameraSession.set_camera_format(), the REST analogue of
                            # tools/control/sweep_camera_format.py), and verify_low_storage.py
                            # (Phase 8 item 1 — real-hardware verification for
                            # wait_for_low_storage(), whose threshold-crossing logic had only
                            # run against the fake-client unit suite; recommends a smaller card
                            # than every other real-hardware run in this repo used, since a
                            # meaningful threshold is otherwise impractical to reach in one
                            # session. Two runs, POCKET_6K_G2 v8.6, 2026-08-05: the "stays
                            # healthy" branch (False after a full 1800s timeout, card
                            # 117.57GB -> 55.02GB without crossing 10GB) and the harder "live
                            # crossing mid-wait" branch (True after 3.1s waiting on the first
                            # real /media/workingset push, not the immediate-return shortcut)
                            # both confirmed; only the shortcut itself has no dedicated run
                            # yet), and diagnose_timeline.py (built for a real select_clip()
                            # failure, POCKET_6K_G2 v8.6, 2026-08-05, then found the actual
                            # mechanism on its first run: POST /timelines/0/add is a no-op
                            # when the target clip's format doesn't match the camera's live
                            # format — no error, no timeline change, no eventual catch-up
                            # even after 30s. Puts every earlier confirmed select_clip()
                            # success in question, in a specific way: all of them switched
                            # format to match the target clip before POSTing, so none can
                            # distinguish "the POST selected the clip" from "the format
                            # switch alone already would have populated the timeline" — see
                            # docs/rest/session.md's select_clip() section, finding #7;
                            # --skip-post isolates the answer directly: switches format via
                            # set_camera_format() then reads the timeline with no DELETE/POST
                            # ever sent. Run, POCKET_6K_G2 v8.6, 2026-08-05: answered — the
                            # format switch alone populated the timeline with the target clip,
                            # zero DELETE/POST sent; POST /timelines/0/add has never been shown
                            # to do anything on this firmware (confirmed for the
                            # single-matching-clip case; the multi-clip
                            # seven-clips-share-a-format scenario remains untested with POST
                            # fully absent). First attempt hit the known select_clip()
                            # sensor-resolution ambiguity on its default clip; the operator
                            # deleted that clip from the card to route around it, then the
                            # re-run against the one remaining clip gave the decisive result),
                            # and verify_playback_interrupt.py
                            # (Phase 8 item 2, part 2 — real-hardware verification for
                            # RestCameraSession.playback_interrupted /
                            # wait_for_playback_interrupt(). Two phases: a self-requested
                            # sanity check (select_clip through stop(), asserting
                            # playback_interrupted stays clear throughout) followed by the
                            # real positive case (play(), then pull the card or press
                            # stop/pause on the camera body, then
                            # wait_for_playback_interrupt()). Follows verify_low_storage.py's
                            # precedent for shape and reporting. First run's sanity phase found
                            # and fixed a real in-flight-guard defect (see session.py's
                            # playback_interrupted entry above); four more runs (--clip-id 2)
                            # confirmed both phases clean end to end each time — phase 1 no
                            # false-positives, phase 2's real interrupt detected in 13.5s/19.1s
                            # (fixed speed==0 trigger) and 6.2s/3.2s (broadened
                            # speed!=_expected_speed trigger, after it replaced the fixed
                            # check) — every interrupt across all 5 runs landed on a full stop,
                            # so the broadening's own "deviates to nonzero" branch is still
                            # real-hardware-unexercised). See docs/rest/transport.md. Also
                            # extended (clip/still deletion investigation, following on from
                            # Phase 10's format_device()): --probe-mounts-delete (safe, opt-in
                            # — DELETEs a synthetic never-real filename in every directory
                            # walk_mounts() already listed, to test whether DELETE is routed
                            # on /mounts/... at all without risking a real file) and
                            # --delete-real-file PATH (destructive, single-target, typed-
                            # filename gated — GET before/DELETE/GET after on one operator-
                            # chosen real file, verdict keyed on the after-GET's status, not
                            # the DELETE response's own). Neither flag adds a capability to
                            # RestCameraSession on its own — a delete_still()/delete_clip()
                            # method is a deliberate next step, not an automatic consequence
                            # of confirmation landing (design principle 6). --probe-mounts-delete
                            # run once for real, POCKET_6K_G2 v8.6, 2026-08-13: inconclusive
                            # (root /mounts/ DELETE got a generic 404 — stock Werkzeug/Flask
                            # NotFound page, not DELETE-specific; the real device directory's
                            # DELETE got a 500, the same boilerplate the known Stills-listing
                            # GET 500 defect returns, but the probed target was synthetic so
                            # this couldn't distinguish a real defect from a routing
                            # question). --delete-real-file's first attempt (against a real
                            # clip, /mounts/A002-sd1/A002_08120218_C001.braw) crashed on a
                            # real tool bug instead of reaching its own DELETE: request()
                            # called response.text() on a .braw file's application/octet-stream
                            # body, which isn't valid UTF-8 — UnicodeDecodeError. Fixed:
                            # decode_body() now falls back to a "<binary body, N bytes>"
                            # placeholder rather than raising (unit-tested against every
                            # single byte value alone, matching RestClient.exists()'s own
                            # binary-safety discipline on the library side). Since the tool
                            # crashed, the operator repeated the same GET/DELETE/GET sequence
                            # by hand in Postman instead — arguably stronger evidence, since
                            # it rules out any bug in the tool's own status interpretation:
                            # 200 (before) -> 200 OK (delete) -> 404 (after). CONFIRMED:
                            # DELETE on /mounts/... really deletes a real clip file on this
                            # camera and firmware. Still deletion specifically remains
                            # independently unconfirmed (the Stills 500 defect means no
                            # still's exact path has been read off a listing the way this
                            # clip's was) — plausibly the same mechanism, not yet proven. See
                            # docs/rest/transport.md's "Mounts DELETE investigation —
                            # CONFIRMED" section for the full write-up
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
  rest_read_state.py        # RestCameraSession's full read surface — format, storage,
                            # clips, timecode, notification-driven is_recording
  rest_record_start_stop.py # Dual-check-verified record start/stop via RestCameraSession
                            # (Phase 4); see docs/rest/session.md
  rest_change_format.py     # BRAW <-> ProRes round trip via RestCameraSession.set_camera_format
                            # (Phase 5, single PUT /system/format vs BLE's up-to-three
                            # packets); see docs/rest/session.md
  capture_photo.py          # Phase 6 — BLE trigger (CameraSession.capture_photo()) +
                            # REST confirmation (rest/media.py); the first script in this
                            # codebase to hold a BLE and a REST session open concurrently
                            # against the same camera. See docs/ble/photo_capture.md §11
  rest_playback.py          # Phase 7 — select_clip + enter_playback/play/pause/seek/
                            # shuttle/stop/exit_playback; entirely new capability BLE
                            # never reached. select_clip() replaces an earlier
                            # set_timeline(clip_unique_ids) design that real-hardware
                            # debugging (POCKET_6K_G2/POCKET_6K_PRO v8.6, 2026-08-04)
                            # disproved outright — the camera has no concept of a
                            # caller-curated playlist; the timeline is always every clip
                            # matching the camera's current format, confirmed two
                            # independent ways (a REST readback and the camera's own
                            # on-screen playback view agreeing on the same seven-clip
                            # group). select_clip() switches format to match the requested
                            # clip and confirms it's a member of the resulting group. This
                            # full script (all nine steps, select_clip through
                            # exit_playback) is now real-hardware-confirmed end to end on
                            # both cameras. A later run finally exercised select_clip()'s
                            # own set_camera_format() switch branch and surfaced an
                            # unexpected finding: the camera's format reverted to its
                            # pre-select_clip() value after exit_playback(), unrequested by
                            # anything in this script. Three follow-up runs isolated the cause
                            # conclusively: select_clip() alone, and select_clip()+
                            # enter_playback() alone, both left the camera at the switched
                            # format with no revert; only removing exit_playback() from the
                            # sequence removed it. CONFIRMED: exit_playback() (stop() is its
                            # alias) restores whatever format preceded playback mode — no
                            # opt-out exposed. Brackets its run with GET /system/format
                            # snapshots (before select_clip, after exit_playback) to make
                            # this visible, and gives stop() (currently an alias for
                            # exit_playback()) a longer dedicated pause so an operator can
                            # actually look at the camera before the redundant
                            # exit_playback step re-fires the same call. Confirmation here
                            # is via this session's own dual-checks; watching the camera
                            # screen is still the independent ground truth, see
                            # docs/rest/session.md and docs/rest/transport.md.
                            # _select_clip_trying_all_sensor_resolutions() (2026-08-05):
                            # select_clip() has no way to disambiguate a clip whose
                            # (codec, resolution, fps) pairs with more than one
                            # sensorResolution and raises BMDUnsupportedError rather than
                            # guess (design principle 7) — kept a library-level restriction
                            # on purpose. This example composes a retry around that strict
                            # boundary instead: catches the error, re-derives the real
                            # candidate sensorResolution values from supported_formats(),
                            # and tries set_camera_format() with each in turn, relying on
                            # select_clip()'s own format check never comparing
                            # sensorResolution so a second call after a candidate is set
                            # proceeds normally. Confirmed here, POCKET_6K_G2 v8.6,
                            # 2026-08-05, the exact ProRes:HQ @ 1920x1080p25 three-way-
                            # ambiguous clip: the first candidate tried succeeded
                            # immediately, and the full remaining sequence passed clean —
                            # also the first real-hardware run of select_clip()'s own
                            # set_camera_format() branch against a genuine mismatch (closes
                            # its docstring's finding #5). Same run confirmed
                            # /transports/0/play and /transports/0/stop push real,
                            # correctly-computed booleans matching Notification.yaml
                            # (Phase 8 item 2 — tools/rest/watch_events.py run alongside);
                            # writes to those two paths remain untested
  check_timeline_stale_entries.py  # Takes two clip_unique_ids of differing format,
                            # switches between them via select_clip(), and reads back
                            # RestCameraSession.timeline_clip_ids() (new read verb) after
                            # each switch to check whether any clip from the first
                            # format's group lingers after switching to the second —
                            # closing the one open question select_clip()'s own
                            # real-hardware runs left behind (whether skipping the broken
                            # DELETE /timelines/0 leaves stale cross-format entries).
                            # Deliberately never calls enter_playback()/exit_playback(),
                            # since exit_playback()'s confirmed format-revert would muddy
                            # the read. CONFIRMED, POCKET_6K_G2 v8.6, 2026-08-04, both
                            # directions (two real format pairs): no stale entries. Causal
                            # reading revised 2026-08-05: both runs switched format before
                            # POSTing, and POST /timelines/0/add is now known to be a no-op
                            # on a format mismatch, so this can't distinguish POST replacing
                            # the timeline from the format switch alone already having done
                            # so — the "no stale entries" result itself still stands. See
                            # docs/rest/session.md's timeline_clip_ids() and select_clip()
                            # sections (finding #7)
  rest_record_test_clip.py  # Sets a (codec, variant, resolution, fps) combination via
                            # set_camera_format(), records a real clip of configurable
                            # length (default 10 minutes), and prints camera
                            # settings/media state/clip inventory at three points — before
                            # any changes (the camera as found), before recording, and
                            # after — then identifies the newly-written clip by diffing clips()
                            # before vs. after (GET /clips/list has no "just-written"
                            # flag), reporting its name (file_path), length
                            # (duration_timecode), and memory used (active storage
                            # device's remaining_space before minus after — Clip carries
                            # no size field of its own, the same gap Phase 6's
                            # rest/media.py hit for stills). Three real-hardware runs
                            # (POCKET_6K_G2 v8.6, 2026-08-05) produced two findings, both
                            # in docs/rest/session.md: remaining_record_time is STALE
                            # immediately after a format change (reports the pre-switch
                            # format's estimate until a recording starts; remaining_space
                            # stays accurate) — confirmed all three runs — which matters for
                            # _require_storage_ready()'s gate on that field; and record_stop
                            # raised BMDVerificationError once on a recording that actually
                            # succeeded (clip count already incremented next run). The two
                            # follow-up runs disproved the original "verification window is
                            # structurally tight" reading — PUT /transports/0/record's
                            # round-trip time (1143-1161ms across all three) was never close
                            # to exhausting the 5s budget — leaving a one-off camera-side
                            # finalization delay (an on-screen warning + timecode flicker
                            # appeared only on the failed run, and no REST property reports
                            # it) as the honest explanation. Fixed anyway: record_stop now
                            # gets its own wider budget, stop_verify_timeout_s (default
                            # verify_timeout_s * 3), with its secondary GET polled instead
                            # of fired once — record_start's single-shot behavior is
                            # unchanged
  rest_format_device.py     # Phase 10 — RestCameraSession.format_device() via the GET-key/
                            # PUT flow. The only media-erasure capability the official BMD
                            # REST spec exposes at all (no per-clip/per-still delete endpoint
                            # in any of the 11 supplied spec files). Layers two safety gates
                            # on top of format_device()'s own mandatory confirm=True,
                            # deliberately departing from every other examples/ script's
                            # plain edit-and-run convention: prints storage_state() first, then
                            # requires typing the exact device name back at a prompt before
                            # anything is sent to the camera. Three real-hardware runs
                            # (POCKET_6K_G2 v8.6, 2026-08-13): the first two confirmed the
                            # capability check, typed-confirmation flow, and GET-key round
                            # trip, then surfaced the filesystem-required defect above via a
                            # real 400, then (after that fix) the volume-required defect via a
                            # second real 400 — neither reached the camera's format logic;
                            # nothing on the card was touched either time. Now prints
                            # doformat_supported_filesystems()'s live result before prompting
                            # and aborts with no request sent if FILESYSTEM is still unset, so
                            # a rerun uses a real camera-offered value rather than a guess;
                            # VOLUME stays None by default and is now resolved from the
                            # device's current name via format_device()'s own storage_state()
                            # lookup rather than sent bare. The THIRD run (both fields
                            # resolved — filesystem="ExFAT", the real camera's casing,
                            # different from the spec's own "ExFat" example) SUCCEEDED end to
                            # end: a real 1TB full-card format completed in ~5s, clip_count
                            # 20 -> 0, remaining_space restored — the first full real-hardware
                            # confirmation of format_device(), including its polling/
                            # completion path — see docs/rest/session.md's Phase 10 section
  rest_delete_clip.py       # Phase 11 — RestCameraSession.delete_clip(). Self-contained
                            # round trip: record_start()/wait_while_recording(10)/
                            # record_stop() a real 10s throwaway clip, confirm_new_clip()
                            # (Phase 9) to identify it, then delete_clip(confirm=True) it.
                            # No interactive typed-confirmation gate, unlike
                            # rest_format_device.py — whatever this script deletes is
                            # guaranteed to be the clip it just recorded in this exact
                            # run, never irreplaceable existing footage, the same
                            # reasoning rest_record_test_clip.py already relies on to
                            # record real clips with no interactive prompt. An earlier
                            # version of this script instead asked the operator to pick
                            # an existing clip from a printed inventory and type its
                            # filename back to confirm — replaced once record-then-delete
                            # made that gate unnecessary rather than kept alongside it.
                            # delete_clip()'s underlying GET/DELETE/GET sequence is
                            # real-hardware-confirmed (POCKET_6K_G2 v8.6, 2026-08-13, via
                            # Postman after probe_endpoints.py's own attempt crashed on a
                            # binary-body bug). This script's own first real-hardware run,
                            # same camera/firmware/day (clip_unique_id=23,
                            # A002_08120328_C003.braw, ~9.9s recorded) SUCCEEDED end to
                            # end: record, confirm_new_clip, delete_clip all confirmed —
                            # and surfaced the /clips/list-staleness finding documented on
                            # delete_clip() above. See docs/rest/session.md's Phase 11
                            # section
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
| `docs/ble/photo_capture.md` | Photo-capture reverse engineering — trigger confirmed on both cameras, no BLE-observable confirmation signal found; Sensor Area BLE investigation (closed, unwritable); Phase 6 — `capture_photo()` built, confirmation moved to REST (`rest/media.py`) |
| `docs/ble/reverse_engineering.md` | Tool-by-tool procedure for bringing up a new `(MODEL_KEY, FIRMWARE)` pair, and for adding a single new command |
| `docs/ble/camera_registry.md` | Full evidentiary notes behind the Camera Registry table above |
| `docs/rest/transport.md` | REST/WebSocket transport (8.6) — addressing the camera over USB, scheme discovery, `tools/rest/probe_endpoints.py`, sweep results for both cameras, the `RestClient`/`RestEventRouter` library surface |
| `docs/rest/session.md` | `RestCameraSession` — the REST state/control surface: read verbs (Phase 3 — format, storage, clips, timecode, notification-driven `is_recording`); `record_start`/`record_stop` (Phase 4 — dual-check verification, storage precondition, real-hardware-confirmed); `set_camera_format` (Phase 5 — one `PUT /system/format`, live-capability-gated via `supported_formats()`); photo-capture confirmation primitives (Phase 6 — `list_mount`/`mount_names`/`path_exists`/`guess_new_still_path`, real-hardware-confirmed); playback/gallery writes (Phase 7 — `select_clip`/`enter_playback`/`play`/`pause`/`seek`/`shuttle`/`stop`, real-hardware-confirmed end to end on both cameras — `select_clip` replaced an earlier `set_timeline` design real hardware disproved outright); codec name mapping and REST timecode decode |

---

## BMD BLE Protocol

Commands are written as binary packets to `OUTGOING_CONTROL`; echoes and responses arrive on `INCOMING_CONTROL`. `docs/ble/protocol.md` is the full reference (packet structure, all SDI categories/parameters, data types, operations, spec-vs-sniffer divergences) and must be read before any protocol work; `docs/ble/packet_structure_and_constants.md` covers the packet header byte layout and `protocol/codec.py` design in detail.

---

## Payload JSON Structure

`payloads/models/<MODEL_KEY>/ble/<FIRMWARE>.json` — validated against `payloads/ble_schema.json` at load time. Every sniffer-confirmed command family gets one block under `commands`: protocol coordinates, a named `values` map, the observed `echo_operation`, and structured `provenance`. See `docs/ble/payload_profiles.md` for the full structure, an example profile, design rationale, and the `CommandSpec` / `require_codec` / `require_resolution` / `require_fps_mode` API.

---

## Storage Media Monitoring *(planned on BLE; built on REST — see design principle 4)*

Design intent only on the BLE side — no storage monitoring is implemented there (BLE's own `StorageState`/`CameraState` do not exist; see design principle 4). Scope is the SD card slot only; external USB media is out of scope entirely. See `docs/ble/recording.md`'s "Storage Media Monitoring — full design intent" section for the complete pre/post-condition table and scope notes, including how REST's `/media/workingset` may change this plan. **REST already has its own, real implementation** (Phase 8 item 1's `last_known_storage`/`wait_for_low_storage()`, Phase 9's `rest/state.py` `CameraState`/`StorageState`, and `confirm_new_clip()`'s post-recording confirmation) — not the BLE plan this section otherwise describes; see `docs/rest/session.md`.

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
- Never reuse a REST `clip_unique_id` across a reconnect — real-hardware-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-05: the same two files reported different `clipUniqueId` values (`15`/`16`, then `1`/`2`) across two separate sessions minutes apart, no reformat or recording in between. Re-read `clips()` fresh in the current session immediately before using an id — see `docs/rest/session.md`'s `clips()` section
