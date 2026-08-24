# REST Session — State and Control Surface (Phases 3-7)

**Status:** read verbs (Phase 3), `record_start`/`record_stop` (Phase 4), and
`set_camera_format` (Phase 5) are all implemented and **real-hardware-confirmed** on both
`POCKET_6K_G2 v8.6` and `POCKET_6K_PRO v8.6`, 2026-08-03. `record_start`/`record_stop`:
6/6 via the dual-check (`examples/rest_record_start_stop.py`), the same run that also
caught and fixed `wait_while_recording`'s return-value-inversion defect (see below).
`set_camera_format`: `examples/rest_change_format.py`'s first run confirmed
`ProRes/422/4K DCI @ 23.98` — the exact combination BLE's write path cannot reach
(`known_unreachable`) — then hit a real `400` switching to `BRAW/5:1/4K DCI @ 23.98`
immediately after (a `sensorResolution` derivation defect, see below); fixed, then
re-confirmed on both cameras back to back. `tools/rest/sweep_camera_format.py`'s first
real-hardware run then confirmed **544/544 (0 unconfirmed) across the full combination
space**, including the "sensor area" dimension — while also catching a second real defect
(`/system/format` was never subscribed for the WS dual-check's primary channel, so every
write silently paid the full verify timeout before confirming via the secondary `GET`
instead), now fixed and **re-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-04**: an identical
full re-sweep (544 confirmed, 16 unsupported, 0 unconfirmed) with per-combination timing
now 0.0–0.2s (a few 1.1s outliers) instead of a uniform ~6.0s — the primary WS-event
channel is genuinely engaging.

`list_mount()`/`mount_names()`/`path_exists()` (Phase 6, `docs/ble/photo_capture.md` §11)
are implemented and unit-tested — the REST half of photo-capture confirmation, composed
with `CameraSession.capture_photo()` (BLE) via `rest/media.py` and
`examples/capture_photo.py`. Real-hardware runs against `POCKET_6K_PRO v8.6`, 2026-08-04,
in order: (1) a `RestClient` defect made every `/mounts/...` call 404; (2) fixed, but the
*design* itself was wrong — `rest/media.py`'s original filename-prediction approach (a
still shares a clip's date-time stem) was disproven by pulling the card, so confirmation
was redesigned around the Stills subdirectory's own `mtime`, and
`path_exists()`/`RestClient.exists()` were removed as dead code once nothing called them;
(3) the redesigned `mtime`-based mechanism **ran clean and confirmed a real capture** —
its first success; `path_exists()`/`RestClient.exists()` were then reintroduced for
`guess_new_still_path()`, an opt-in, purely informational filename lookup layered on top
of the (already-succeeding) `mtime` confirmation — never a substitute for it; (4)
`guess_new_still_path()`'s own first real-hardware runs found a real defect in *it*: two
captures roughly 30 seconds apart landed in the same clock-minute, so both matched the
same timestamp candidate, and the original ascending-index search returned the same
*stale* (lower, already-existing) filename both times instead of the just-written one.
Fixed by always checking the highest candidate index first — since the counter only
grows, the highest matching index under a given timestamp is always the most recent
capture; (5) **re-confirmed on real hardware immediately after the fix** — two more
back-to-back captures, again in the same clock-minute, correctly returned two distinct,
increasing filenames (`..._S009.braw`, then `..._S010.braw`) instead of repeating. See
"`list_mount(path)` → `tuple[dict, ...]` / `mount_names()` → `tuple[str, ...]`" and
"`path_exists(path)` → `bool` and `guess_new_still_path()`" below, and
`docs/ble/photo_capture.md` §11, for the full evidentiary record.

`select_clip()`, `enter_playback()`/`exit_playback()`, `play()`/`pause()`/`stop()`, and
`shuttle()`/`seek()` (Phase 7 — playback and gallery, entirely new capability BLE never
reached) are implemented, unit-tested, and have a new `examples/rest_playback.py`.
**The full sequence is now real-hardware-confirmed end to end on both cameras** — see the
fourth finding below; the three findings before it are the debugging trail that got there.

**First real-hardware run, `POCKET_6K_G2 v8.6`, 2026-08-04, against the original
`set_timeline(clip_unique_ids: list[int])` design:** hit two real defects back to back on
its very first call. `DELETE /timelines/0` returns `501` on this firmware, not just
theoretically unswept but actually confirmed unimplemented — fixed by catching that
specific `501` and proceeding straight to `POST`ing. The `POST` itself then came back
`400 {"error": "Invalid clips data"}` against the original one-request-per-clip,
bare-`{"clipUniqueId": id}` body — fixed by switching to one request carrying all clips
under a `"clips"` key, matching `GET /timelines/0`'s own confirmed response shape. The run
stopped there, so `enter_playback()` onward remained unexercised by this run.

**Second real-hardware evidence, `POCKET_6K_PRO v8.6`, same day, gathered by operator
testing directly rather than through this script:** `PUT /transports/0/playback`'s real
body is `{"type": "Play", "loop": bool, "singleClip": bool, "speed": float,
"position": int}` — the migration plan's `"speed"`-only hypothesis was incomplete, and its
`"Shuttle"`/`"Jog"` guess for `type` and `"timecode"`/`"clip"` guess for the position field
were both wrong. `shuttle()`/`seek()`/`_put_playback()` are rewritten around the real
shape as a read-modify-write (see "`shuttle(speed)` / `seek(position)`" below).

**Third real-hardware evidence, same session, the finding that forced a redesign:** with
the "clips" body fix from the first round applied, and the camera's format confirmed
`ProRes:Proxy` at `4096x2160p24` immediately beforehand, `POST`ing
`{"clips": [{"clipUniqueId": 1}]}` produced a `GET /timelines/0` readback of **seven**
clips — every clip on the card sharing that exact format, not just clip `1`. Repeating the
identical request with `clipUniqueId: 5` instead of `1` (format re-verified unchanged, no
camera-body interaction in between) produced the *identical* seven-clip set. Independently,
the camera's own on-screen playback view (photographed live) showed `"CLIP 1/7"` for the
same group, confirming this is native camera behavior, not a REST quirk: **the camera has
no concept of a caller-curated playlist. The timeline is always every clip matching the
camera's current format, and the requested `clipUniqueId` does not select which clips are
in it.** `set_timeline(clip_unique_ids: list[int])`'s whole premise — pass an arbitrary
ordered subset, get exactly that — doesn't describe how this camera works, so it's
replaced outright by `select_clip(clip_unique_id: int)`: pick one clip, let its format
define the resulting group, confirm the requested clip is a member of whatever comes back
rather than the sole entry. See `select_clip()`'s own section below for the full trail.

**Fourth real-hardware run, `POCKET_6K_G2` and `POCKET_6K_PRO v8.6`, 2026-08-04, the first
run of `select_clip()` itself:** `python examples/rest_playback.py` ran the entire Phase 7
sequence — `select_clip()` -> `enter_playback()` -> `play()` -> `pause()` -> `seek(0)` ->
`shuttle(2.0)` forward -> `shuttle(-1.0)` backward -> `stop()` -> `exit_playback()` — start
to finish on **both** cameras, every step's own dual-check passing (`select_clip()`'s
readback poll, `_set_transport_mode()`'s and `_put_playback()`'s WS-event/GET dual-checks).
This answers the open question the third finding left behind: the format-switch-then-sync
combination `select_clip()` composes does work on real hardware — at least for the tested
case (`clip_unique_id=1`, whose format already matched the camera's current setting on
both runs, so this specific run didn't exercise the `set_camera_format()` branch inside
`select_clip()`; that piece's evidence is still Phase 5's own). `2.0`/`-1.0` `shuttle()`
magnitudes and `seek(0)` are real-hardware-confirmed for the first time here too — see
their own docstrings.

Two of the four endpoints Phase 7 needs
(`/transports/0` and `/transports/0/playback`) have a real same-value-`PUT`-confirmed
`204` from the Phase 0 sweep (`docs/rest/transport.md`), and `/transports/0/playback` now
also has a real changing-write sample (above); the other two
(`/timelines/0`/`/timelines/0/add`, needed by `select_clip()`) were never even
same-value-probed — `probe_endpoints.py`'s `NEVER_WRITE` list skips them entirely, the same
position `/transports/0/record` was in before Phase 4 proved it out. `play()`/`stop()` are
deliberately built as aliases (`shuttle(1.0)`/`exit_playback()`) rather than touching the
dedicated `/transports/0/play`/`/transports/0/stop` triggers, which have the same
zero-write-evidence problem as the timeline endpoints — see "`play()`/`pause()`/`stop()`"
below for the full reasoning.

## Overview

`rest/session.py`'s `RestCameraSession` is the REST analogue of `ble/session.py`'s
`CameraSession` — it composes `RestClient` (transport) and `RestEventRouter` (WS event
buffering) exactly as `CameraSession` composes `BMDCameraController` and
`NotificationRouter`, holding the same transport/protocol separation design principle 5
draws for BLE.

```python
async with RestCameraSession("172.27.97.141", "POCKET_6K_PRO", "v8.6") as session:
    fmt = await session.get_format()
    storage = await session.storage_state()
    clips = await session.clips()
    tc = await session.timecode()
    print(session.is_recording)

    await session.record_start()
    await session.wait_while_recording(timeout=5)
    await session.record_stop()

    await session.set_camera_format("BRAW", "5:1", "4K DCI", "23.98")
```

`examples/rest_read_state.py` exercises the read verbs; `examples/rest_record_start_stop.py`
exercises `record_start`/`record_stop` across several cycles, mirroring the BLE
`examples/record_start_stop.py`; `examples/rest_change_format.py` exercises
`set_camera_format`, mirroring `examples/change_codec.py`. `examples/rest_record_test_clip.py`
composes several of these together end to end — `set_camera_format`, a full-length
`record_start`/`wait_while_recording`/`record_stop` cycle, and `storage_state`/`clips`
snapshots taken before and after — to record a real test clip at a chosen combination and
report the clip it produced (name, length, storage consumed), identified by diffing
`clips()` before against after since `GET /clips/list` has no "just-written" flag of its
own. No new library surface. It prints state at three points, not two — "BEFORE any
changes" (the camera as found, before `set_camera_format` touches anything), "BEFORE
recording", and "AFTER recording" — which is what surfaced the stale-estimate finding
below.

**Real-hardware findings, `POCKET_6K_G2 v8.6`, 2026-08-05** (first run of this script
against a camera; BRAW 5:1 / 4K DCI / 23.98, 60s, 1TB card with 996GB free):

1. **`remaining_record_time` is stale immediately after a format change.** Read straight
   after `set_camera_format()` returned confirmed, the active device reported `50858`s
   (14h07m) — implying ~19.6 MB/s, the *pre-switch* format's bitrate. After the recording,
   the same card with 992.48 GB free reported `15251`s (4h14m), implying ~65 MB/s, which
   matches the clip actually written (3525050368 B / 61 s = 57.8 MB/s). The camera had not
   recomputed the estimate for the new format. `remaining_space` stayed accurate
   throughout. This matters for `_require_storage_ready()`, which gated `record_start()` on
   `remaining_record_time > 0` — read it as a pre-switch leftover, not a current number,
   until a recording has started. **Closed, Phase 9**: `_require_storage_ready()` now gates
   on `remaining_space` alone, the field confirmed accurate in every run to date, including
   this one — see the `record_start()`/`record_stop()` section below for the full reasoning.

2. **`record_stop` raised `BMDVerificationError` once on a recording that actually
   succeeded.** The first run's `record_stop` raised, but the clip was written and saved —
   the next run's own "before" state showed the clip count had gone 2 -> 3. The *original*
   write-up here read this as the verification window being structurally tight, based on
   the second run's `PUT /transports/0/record` taking 1143 ms to return (vs.
   `record_start`'s 2 ms) — reasoning that a finalization slower than that could plausibly
   miss a ~5s budget. **Two further runs the same day correct that reading**: `PUT` took
   1161 ms and 1159 ms respectively, both stops confirmed via the WS primary within the
   original 5.0s budget with no retry needed, and both are far short of it. `PUT`'s
   round-trip time alone was never close to exhausting `verify_timeout_s` — it was never
   the bottleneck the first write-up implied. Across three runs, only one failed, and the
   operator observed a warning indicator and flickering timecode on the camera's screen
   *during that one run only* — the honest reading is a one-off, camera-side finalization
   delay (possibly the same event behind that on-screen warning), not a timing budget that
   is tight in general. **No REST property reports whatever that warning was** — nothing in
   either camera's 46/48 subscribable properties covers write margin or dropped frames, so
   the camera displayed something the API never exposed. This is the same gap Phase 8's
   frame-drop item records as unavailable.

   **Fix applied regardless** (`session.py`, same day): even though three runs suggest the
   default budget is usually enough, an occasional finalization anomaly with no REST-visible
   cause is exactly the case a single-shot secondary check handles worst — one unlucky GET
   right as the state flips is a real race, independent of how rare the anomaly is.
   `record_stop` now gets its own wider overall budget, `stop_verify_timeout_s` (constructor
   parameter, defaults to `verify_timeout_s * 3` = `15.0`s), and its secondary `GET` is
   polled every `0.5`s for whatever time remains after the primary WS wait, instead of fired
   once — the same poll-until-deadline shape `select_clip()` already uses for its timeline
   membership check (`session.py:1167`). `record_start`'s behavior is unchanged: its overall
   budget still equals `verify_timeout_s`, which reduces the poll loop to exactly one `GET`,
   same as before this change. See "`record_start()` / `record_stop()`" below for the
   library-surface description.

Every read verb (`get_format`, `supported_formats`, `storage_state`, `clips`, `timecode`,
`clip_timecode`)
is a plain `GET`, fetched fresh on every call — there is no background cache to go stale.
`is_recording` and `last_known_storage` are the two exceptions: both are tracked
continuously from WS `propertyValueChanged` events (`/transports/0/record` and
`/media/workingset` respectively), mirroring the BLE `CameraSession`'s `is_recording`
attribute (notification-derived only, never inferred from a request this session itself
made — design principle 4).

---

## Connection lifecycle

`session` may be injected (real or fake) into the constructor for testing, mirroring
`RestClient`'s own constructor — an injected session is never this session's to close.
When no `session` is given, `__aenter__` creates its own `aiohttp.ClientSession`, and
`__aexit__` closes it. This ownership rule holds on *every* exit path, not just the
success one:

**Real-hardware-confirmed leak, `POCKET_6K_G2 v8.6`, 2026-08-03**: the first version of
`__aenter__` created the owned `aiohttp.ClientSession` and then called
`RestEventRouter.connect()`. When that WS connect failed (a `ClientConnectorDNSError` —
the host didn't resolve), `__aenter__` raised without returning, so Python never called
`__aexit__` — the session it had already created went unclosed
(`ERROR - Unclosed client session`, aiohttp's own leak warning). `__aenter__` now tracks
whether every setup step succeeded and closes an owned session in a `finally` block on
any failure, without catching/masking the original exception — the same "cleanup on
failure, propagate the real error" shape as `except Exception: cleanup(); raise` gets you,
without the broad `except` "What Not To Do" forbids.

**`__aenter__` subscribes to both `/transports/0/record` and `/system/format`**, giving
every write method's dual-check a standing WS subscription for its primary channel —
neither `record_start`/`record_stop` nor `set_camera_format` subscribes anything itself,
they only `arm()`/`wait_for()` on a property this method already subscribed. See
`set_camera_format`'s "Write verbs" entry below for the real-hardware defect that exposed
`/system/format` never being subscribed at all in the first version of this method.

### `reconnect()` — force a fresh connection without losing the session object (Phase 14)

`GET /clips/list` (and `storage_state().active_device.clip_count`) can go stale within
the same session immediately after `delete_clip()`/`delete_clips()` — real-hardware-
confirmed, `POCKET_6K_G2 v8.6`, 2026-08-13/14 (`delete_clip()`'s own docstring/section
has the full trail). The only mechanism confirmed to *reliably* clear it is a fresh
reconnect — at the time this method was designed, confirmed exactly once, via a genuinely
separate process (`examples/rest_read_state.py`, run standalone roughly 48s after the
deleting run's own process had already exited). A later run separately found the
staleness can sometimes self-clear within the same session, unreconnected, in well under
two seconds — one stale entry out of three, a different and non-deterministic mechanism,
not a second confirmation of reconnecting itself. Until this method existed, "reconnect"
meant discarding the whole `RestCameraSession` object and starting a new script/process —
nothing in this repo had ever constructed a second session or re-entered one within a
single process. `reconnect()` tears down and rebuilds the transport on the *same*
object instead, so a caller never has to swap which reference it holds. **Now confirmed a
second time**, via `reconnect()` itself — see the real-hardware run below, which also
sharpened the finding: the reconnect only took ~1.16s, not anywhere near 48s, and
staleness was already gone immediately afterward.

**Not `/clips/list`-specific.** Any field this session tracks only from notifications
reflects only what arrived on the connection about to be replaced. Design principle 4
requires state to be updated only from a real observed notification, never assumed — a
reconnect is, definitionally, a gap where this session observed nothing. `reconnect()`
resets every field `_reset_connection_state()` owns to its `__init__` default *before*
the new connection opens: `is_recording`, `last_known_storage`, `_in_playback`,
`last_known_play`, `last_known_stop`, and a **brand-new** `playback_interrupted`
`asyncio.Event` instance (not merely cleared) on `CameraState`, plus
`_recording_stopped`, the low-storage arming trio, and the playback/transport-mode
write-in-flight trio. Plain config (`host`/`scheme`/`port`/`profile`/the four timeouts)
is untouched — none of it lives in `_reset_connection_state()`.

**Mechanics**: `await self.__aexit__()` then `await self.__aenter__()` — reused
verbatim, not duplicated. An injected (non-owned) session is preserved and reused
exactly as the existing `_owns_session` contract above already dictates; only an owned
session is closed and replaced. `RestEventRouter.connect()` already auto-resubscribes
every property in its own `_subscribed` set on top of `__aenter__`'s own 7 explicit
`subscribe()` calls — 14 WS subscribe sends per `reconnect()` instead of 7. Left as-is:
harmless (a duplicate subscribe is a camera-side no-op) and bounded (`_subscribed` is a
`set`, so it doesn't compound across repeated reconnects) — fixing it would mean
reaching into the router's private `_subscribed` from `session.py`, a boundary this file
has never crossed. `RestEventRouter`'s own buffered event state (`_latest`/`_seq`/
`_armed_baseline`/`_armed_snapshot_value`) is also left untouched for the same
reason it's harmless: every write method's dual-check calls `arm()` fresh, immediately
before its own request, so a leftover pre-reconnect buffered value is only ever a
discarded baseline, never surfaced to a caller directly.

**Caveats**: a coroutine already blocked on the *old* `playback_interrupted` Event
object (fetched before `reconnect()` was called) is never woken — re-fetch the property
afterward rather than holding a reference across the call. `reconnect()` takes no lock
and does not check `_playback_write_in_flight`/`_transport_mode_write_in_flight` before
tearing down the transport — calling it concurrently with any other in-flight write/wait
on the same session is undefined, the same caller-owns-sequencing discipline every write
method here already relies on, but a genuinely new hazard: previously, reconnecting
always meant a separate process, so nothing could ever race one.

**Status: real-hardware-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-14, first run.** Unit-tested
against a fake transport (`TestConnectionLifecycle` in `tests/unit/rest/test_rest_session.py`) —
transport rebuild, injected-session reuse, owned-session replacement, full state reset, and
resubscription all covered — then `examples/rest_reconnect_after_delete.py` succeeded end to
end on its very first real-hardware attempt, no defects found: recorded and deleted a real
clip (`clip_unique_id=12`) — `clips()` immediately after deletion reported `3` (the known
same-session staleness, expected), `id(session)` was identical before and after `reconnect()`
(continuity confirmed), and `clips()`/`storage_state().clip_count` both read `2` immediately
after — `clip_unique_id=12` genuinely gone, no longer counted.

**New finding, sharpening the original staleness-clearing evidence**: the reconnect itself
(`Disconnected` → `Connected` in the log) took **~1.16s**, not anywhere near the ~48s gap between
processes that the original separate-process confirmation happened to have. Staleness was
already gone on the very first `clips()` call after that ~1.16s reconnect completed — no
additional wait beyond the reconnect itself. This answers a question the original finding left
open: it's the *act* of reconnecting that clears the staleness, not elapsed time — a fast,
sub-two-second `reconnect()` is exactly as effective as a 48-second gap between separate
processes.

---

## Read verbs

### `get_format()` → `Format`

`GET /system/format`, parsed into a frozen `Format` dataclass: `codec`, `frame_rate`,
`record_resolution`/`sensor_resolution` (as `(width, height)` tuples), and the
`off_speed_*` fields. `codec` and `frame_rate` are REST's own spelling — see "Codec name
mapping" below for translating to/from the BLE profile's vocabulary.

Real example (`POCKET_6K_PRO v8.6`, 2026-08-04):

```python
Format(codec='ProRes:Proxy', frame_rate='23.98', record_resolution=(4096, 2160),
       sensor_resolution=(5744, 3024), off_speed_enabled=False,
       off_speed_frame_rate=24, min_off_speed_frame_rate=5, max_off_speed_frame_rate=60)
```

**Useful immediately, before any REST write exists**: a BLE `CameraSession` can
cross-check itself against `get_format()` over REST — the secondary check design
principle 3 has always specified for BLE (echo + `CAMERA_STATUS`) never had an
independent-transport equivalent until now.

### `supported_formats()` → `tuple[SupportedFormat, ...]`

`GET /system/supportedFormats` — the camera's own capability matrix, making the BLE
profile's hand-maintained `codecs`/`resolutions`/`fps_modes` tables redundant on the REST
path (docs/rest/transport.md). Raises `BMDUnsupportedError` immediately, without any
request, unless the target camera/firmware's `rest/<fw>.json` profile confirms this
endpoint is `supported` — the REST sibling of design principle 7's capability check.

### `storage_state()` → `StorageState`

`GET /media/workingset` + `GET /media/active`, combined into a `StorageState` — **the
first real implementation of design principle 10**. `StorageState.devices` is the full
fixed-size working set, including empty slots (`device_name == ""`); `active_device` is
resolved from `GET /media/active`'s `workingsetIndex`, never assumed to be index 0 — real
evidence showed the active disk sitting at index 1 with index 0 empty
(docs/rest/transport.md, "why 'don't index slot 0' was right").

### `clips()` → `tuple[Clip, ...]`

`GET /clips/list`. The real response key is `clipList`, not `clips` as the field's own
name might suggest — an assumption this repo already got wrong once
(docs/rest/transport.md). No stills appear here and there is no file-size field, so the
photo-capture confirmation design (Phase 6) cannot lean on this endpoint alone.

**Real-hardware-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-03**: with no SD card inserted,
`GET /clips/list` returns `404 {"error": "No disk or media"}` rather than an empty
`clipList` — a real, informative error, not a broken endpoint. `clips()` catches this
specific case (`BMDRestError.status == 404`, checked structurally via the attribute
`RestClient` now attaches to the exception, never by string-matching the message) and
re-raises `BMDStorageError` instead — exactly what that exception exists to name (design
principle 10), rather than a misleading empty tuple or a generic `BMDRestError` the
caller has to decode by hand. Any other `BMDRestError` (a real `5xx` firmware defect,
say) propagates unchanged — only a confirmed-404-means-no-media translation is made, not
a blanket "swallow every REST error" policy. `examples/rest_read_state.py` demonstrates
the intended catch.

This is the first place a REST read verb needed storage awareness at all; `storage_state()`
itself never raises on a missing card — `/media/workingset` and `/media/active` keep
returning valid (if all-empty) data (confirmed the same run), which is exactly why
`storage_state()`'s `active_device` is designed to come back `None` rather than error.

**Real-hardware finding — `clipUniqueId` is not stable across sessions, `POCKET_6K_G2 v8.6`,
2026-08-05.** Two separate `GET /clips/list` reads, minutes apart, no reformat or recording
in between, on the identical two files (`A005_08051343_C001.mov`, `A005_08051345_C002.braw`,
same card volume `A005` both times): the first read reported `clipUniqueId=15` and `16`;
the second reported `clipUniqueId=1` and `2` for the exact same two files. No confirmed
mechanism for *why* — logged here as a raw, repeatable observation, not a theory. Practical
consequence: **a `clip_unique_id` captured from one `clips()` read must not be reused after
reconnecting** — every caller in this codebase that takes a `clip_unique_id` as an argument
(`select_clip()`, `tools/rest/diagnose_timeline.py`, `examples/check_timeline_stale_entries.py`)
assumes the id a prior session read is still valid; this finding says it might not be.
Re-read `clips()` fresh within the same connected session immediately before using an id,
rather than caching one across a reconnect.

### `timecode()` → `Timecode`

`GET /transports/0/timecode`, decoded via `rest/timecode.py`'s `decode_rest_timecode` —
see "Timecode decode" below. Reuses the BLE `Timecode` dataclass and `duration_seconds()`
as-is; only the wire decode differs between transports.

### `clip_timecode()` → `Timecode`

`GET /transports/0/timecode`'s `clip` field — position within the current clip rather than
time-of-day — decoded with the exact same `decode_rest_timecode` function as `timecode()`.
Confirmed by the official `Notification.yaml` spec and real hardware; see the "Real-hardware
finding" under `is_recording`/`wait_while_recording()` below for the full decode
confirmation and why, like `timecode()`, it cannot show a dropped frame.

### `list_mount(path)` → `tuple[dict, ...]` / `mount_names()` → `tuple[str, ...]`

Phase 6's mount-listing primitives (`docs/ble/photo_capture.md` §11, `rest/media.py`),
added directly to `RestCameraSession` rather than as free functions, since both need the
same connected `RestClient` every other read verb already uses.

`list_mount(path)` is the general form: a raw directory listing at `path`, entries exactly
as the camera reports them (`{"name": ..., "type": "file"|"directory", "mtime": ...}`,
plus `"size"` for files — `docs/rest/transport.md`). `path` can be the bare `/mounts/`
root or a specific mount's own root; both are outside `/control/api/v1`, so this always
calls the client with `api_prefixed=False`. `mount_names()` is `list_mount(MOUNTS_PATH)`
filtered to directory names — the mechanism `rest/media.py`'s `resolve_mount_path()` uses
instead of guessing a mount name from `deviceName` (see that module's docstring for why
the one observed `sd0`→`sd1` mapping is not trusted as a rule). `rest/media.py`'s
`stills_marker()` calls `list_mount()` directly on a resolved mount root to read the
`Stills` entry's own `mtime` — see below.

**Defect found and fixed on real hardware, 2026-08-04 (first run):** the first
`examples/capture_photo.py` run against `POCKET_6K_PRO v8.6` got past `storage_state()`
and the (then-existing) clip-based prefix derivation cleanly, then `mount_names()` raised
`BMDRestError: GET /mounts/ -> 404`. Root cause: `RestClient._request()` unconditionally
prepended `API_BASE` (`/control/api/v1`) to every path, so the real request went to
`/control/api/v1/mounts/` — a path that was never real. `/mounts/` is the Web Media
Manager, a URL namespace at the host root, entirely separate from the control API;
`tools/rest/probe_endpoints.py`'s own two request-building call sites already drew this
distinction (`walk_mounts()` never adds `API_BASE`), but `RestClient` didn't carry the
same rule. Fixed by adding `api_prefixed: bool = True` to `RestClient.get()` (and the
shared `_request()`/`_url()` internals); `list_mount()` now passes `api_prefixed=False`.
Regression tests assert the real constructed URL in both `test_client.py`
(`TestStatusHandling.test_api_prefixed_false_omits_api_base`) and `test_rest_session.py`
(`FakeRestClient.api_prefixed_calls`), since the original tests only asserted status-code
handling and never the URL a mounts call actually produced — which is exactly how this
escaped review.

**Second defect found on real hardware, same day, past the first fix:** with the 404
fixed, the *next* run got all the way through — `mount_names()`, the BLE trigger, the
whole confirmation poll — and reported "NOT confirmed", yet pulling the SD card
afterward showed the photo really had been taken. `rest/media.py`'s original design
assumed a still shares a clip's full `<reel>_<date>` filename stem; the pulled card
showed three real stills where only the reel identifier (`A001`) was actually shared —
the timestamp segment was unique per photo, and the trailing counter had no knowable
baseline (Stills can never be listed — see below). No REST call was broken this time; the
*design* was wrong. Retired the filename-prediction approach entirely
(`derive_still_prefix()`, `find_highest_still_index()`, the old `wait_for_new_still()`) in
favor of watching the Stills subdirectory's own `mtime` in a mount-root `list_mount()`
call — see `rest/media.py`'s module docstring and `docs/ble/photo_capture.md` §11 for the
full evidentiary record. `path_exists()` on `RestCameraSession` and `RestClient.exists()`
were removed once the redesign left them with no caller — dead code, not kept "for later."

**Third real-hardware run, same camera, same day: the redesigned `mtime` mechanism
worked.** `examples/capture_photo.py` ran end to end and reported "Confirmed ✓" — the
first successful run of Phase 6's confirmation design.

### `path_exists(path)` → `bool` and `guess_new_still_path()`

The user then asked for the still's actual filename. `path_exists()`/`RestClient.exists()`
were reintroduced for exactly this — a genuine caller this time, not a hypothetical one.
`path_exists(path)` checks whether a path exists **without ever reading or decoding its
body** (delegating to `RestClient.exists()`), since a still image (`.dng`/`.braw`) is
binary content `get()`'s JSON-then-text fallback could raise decoding on.

`rest/media.py`'s `guess_new_still_path()` uses it to make one **opt-in, best-effort**
attempt at the exact filename, built on the same real-hardware evidence that motivated the
redesign: a still's name is `<reel>_<MMDDHHMM>_S<NNN><ext>`, where the reel is already
known (the mount name's own prefix) and the timestamp lands on the trigger's own minute
(confirmed: a trigger logged at `11:26:24` produced a still stamped exactly `08041126`).
Only the counter `<NNN>` is genuinely unknowable without a listing, so the function probes
a narrow, caller-controlled window of `(minute offset, index, extension)` combinations —
default `range(1, 11)` for the index, deliberately small since an unbounded search would
be slow and still often come back empty on a heavily-used card. It **never gates
`wait_for_new_still()`'s own pass/fail** — a `None` result means only that this specific
guess missed, not that the capture failed. `examples/capture_photo.py` calls it once,
after confirmation, purely to print a likely name.

**Defect found on real hardware, same day, first two runs of this function:** two photos
taken about 30 seconds apart both landed in the same clock-minute, so both matched the
same `(reel, timestamp)` candidate — and the function, checking `index_candidates` in
ascending order, returned the *lower*, already-existing index (`S006`) both times instead
of the newly written one (`S007` the second time), even though `wait_for_new_still()`'s
own `mtime` check correctly confirmed a real new file each time. Not a confirmation bug —
`wait_for_new_still()` was right both times — but a guess bug: since the `_S<NNN>` counter
only ever grows, the highest index that matches a given timestamp candidate is always the
most recently captured one. Fixed by checking `index_candidates` highest-first,
unconditionally, regardless of the order a caller passes them in.

**Re-confirmed on real hardware immediately after the fix:** two more back-to-back
captures, again landing in the same clock-minute, correctly returned two distinct,
increasing filenames (`A001_08041223_S009.braw`, then `A001_08041224_S010.braw`) —
no repeat.

### `is_recording` / `wait_while_recording(timeout)`

**Backed by `CameraState` since Phase 9** — `session.is_recording` is now a property
reading/writing `session.state.is_recording`; no external interface change, everything
below still holds exactly as described. See `state.py`'s module docstring for the full
extraction (`is_recording`, `last_known_storage`, `playback_interrupted`,
`last_known_play`/`last_known_stop`, and `_in_playback` all moved the same way).

`is_recording` starts `None` and updates only from a well-formed
`/transports/0/record` `propertyValueChanged` event's `{"recording": bool}` value —
`__aenter__` subscribes to this property automatically, so it is live for the whole
session lifetime with no extra call needed. `wait_while_recording(timeout)` mirrors the
BLE `CameraSession.wait_while_recording`'s return-value contract exactly: `True` if still
recording (or state unknown) when `timeout` elapses, `False` if a stop is confirmed
before then — the case a caller uses to notice a camera-initiated stop and move on instead
of blindly sleeping out a planned duration.

**Real-hardware-confirmed defect, `POCKET_6K_G2`/`POCKET_6K_PRO v8.6`, 2026-08-03**: the
first version of `wait_while_recording` had the *opposite* contract — `True` for
"confirmed stopped", `False` for "still recording after timeout" — while
`examples/rest_record_start_stop.py` used the same `if not held: stopped_early = True`
pattern as the BLE example, which assumes `CameraSession`'s (correct) contract. Every
cycle's `record_start` → `wait_while_recording` → `record_stop` sequence — which never
sends an explicit stop before this call — misreported "recording stopped before the
requested 5s" on **every single cycle** (6/6 across both cameras), even though the
operator confirmed on the camera's own screen that each recording ran the full requested
duration. `record_start`/`record_stop` themselves were unaffected — both cameras confirmed
6/6 start and 6/6 stop via the dual-check — this was purely `wait_while_recording`'s return
value disagreeing with what it actually observed. Fixed by inverting the two return points
to match `CameraSession`'s contract; also fixed in the same pass: the internal stop-event
flag is now cleared before each wait, closing a latent staleness gap where a flag set by an
*earlier* cycle's stop (possible when that cycle's `record_start()` confirmed via the
secondary `GET` readback rather than the primary WS event, since only the event path clears
it) could have been mistaken for a fresh stop in a later cycle.

**Confirmed against real hardware, 2026-08-03** (`examples/rest_read_state.py`,
`POCKET_6K_PRO v8.6`): every `GET`-based read verb (`get_format`, `supported_formats`,
`storage_state`, `clips`, `timecode`) returned correct real data, but `is_recording` was
still `None` when printed — connect, subscribe, and five `GET`s all completed before any
`/transports/0/record` event arrived. This is design principle 4 working as intended, not
a bug: `is_recording` is never inferred or polled, only set from a genuine event, so a
caller that needs a value *now* rather than eventually should not assume one is available
immediately after connecting. Whether the camera pushes an initial "current value" event
for this property at all (some properties, e.g. `/system/format` and `/media/workingset`,
were observed doing so in the `watch_events.py` run in "Library surface (Phase 2)" above,
tens of seconds after subscribing) or only pushes on an actual state *change* is not yet
disambiguated for `/transports/0/record` specifically — a caller wanting a same-camera
cross-check today should read the transport mode/state directly rather than relying on
`is_recording` being populated on connect.

**Real-hardware finding — the camera's own dropped-frame policy, and how it does (and
doesn't) surface over REST, `POCKET_6K_G2 v8.6`, 2026-08-05.** The camera body has a
`RECORD` settings page field, **"IF CARD DROPS FRAME"**, with two values: `Alert` (show a
warning, keep recording) and `Stop Recording`. This session's camera had it set to
`Stop Recording`. **No endpoint or subscribable property anywhere in this profile's swept
surface exposes this setting or a drop-frame count** — checked directly against all 76
swept endpoint paths and all 46 `websocket_properties`; there is no `dropFrame`,
`timelapse`, `detailSharpening`, or `recordLutToClip` path of any kind (the other fields on
that same settings page), confirming this is a body-menu-only setting with zero REST
surface, not something merely unswept.

Two back-to-back runs at a deliberately demanding combination (`BRAW 3:1` / `6K`
(6144x3456) — the least-compressed BRAW variant at the largest resolution this profile
offers) reproduced the policy directly:

- **`50fps`**: `/transports/0/record` went `True` at `11:00:12.593` and `False` at
  `11:00:15.124` — a ~2.5s recording, nowhere near the requested 300s.
  `wait_while_recording(300)` correctly returned `False` (early stop), `record_stop()`
  correctly no-op'd (design principle 4: `is_recording` was already `False` from the real
  event), and `clips()` reported one real new clip: 00:00:01:06 long, 283MB. The same
  combination at `23.98fps` had already run clean for the full 300s twice with no issue —
  isolating the trigger to fps, not the codec/resolution alone.
- **`30fps`**: same shape, `True` at `11:00:45.127` -> `False` at `11:00:47.944` (~2.8s),
  `wait_while_recording` again correctly returned early — but this time **`clips()`
  reported no new clip at all**. The recording was short-lived enough that nothing was ever
  indexed as a listed clip, a real edge case worth carrying forward: a caller diffing
  `clips()` before/after can silently conclude "nothing happened" on a drop-triggered stop
  this fast, even though the camera genuinely attempted and then aborted a recording.

**Third run, policy switched to `Alert`, same day**: identical combination (`BRAW 3:1` /
`6K` / `50fps`, the exact one that triggered `Stop Recording` above) recorded for the full
300s. `/transports/0/record` went `True` at `11:10:59.468` and `False` at `11:16:02.240`
(~302.8s, matching the requested duration plus the explicit stop call's own overhead) —
no early return from `wait_while_recording(300)`, `record_stop()` confirmed cleanly via
the primary WS event (not a no-op this time), and `clips()` reported one real new clip:
`00:05:01:05`, 7.66GB. Across the full 302-second run, the entire subscribed 46-property
feed (dense `/transports/0/timecode` pushes roughly every 100ms, `/media/workingset` every
~21s) shows **nothing outside the routine set** — no property fired that hadn't already
fired identically at connect-time lens/aperture stabilization in every other run.

**Timecode itself checked directly and cleared, not just assumed.** Decoding the
confirmed `timecode` field (BCD `HH:MM:SS:FF`, per `rest/timecode.py`) across all 3,281
pushes in this exact run's recording window shows zero backwards jumps and zero gaps
beyond the normal ~100ms polling cadence — a perfectly smooth, monotonic advance from
`11:10:51:33` through `11:15:52:35`, straight through the moment a real, operator-witnessed
drop occurred. This confirms directly, not just by design-level reasoning, that `timecode`
is a wall-clock/frame-rate position counter that advances independent of whether the
encoder actually wrote every frame — it structurally cannot show a drop, even one that
demonstrably happened.

**Correction**: an earlier version of this finding treated the event body's second field,
`clip`, as raw/undecoded and declined to read anything into its irregular-looking values.
That was wrong — `clip` is BCD-packed in the exact same `HH:MM:SS:FF` format as `timecode`
(the official `Notification.yaml` AsyncAPI spec confirms this directly: "The position of
the clip timecode in units of binary-coded decimal (BCD)"), decodable with the identical
function. Decoded properly across the same 3,281 pushes: every sample's four BCD nibbles
are valid digits, the value resets near zero right at `record_start` and advances smoothly
and monotonically to `05:01:01`, matching the clip's own reported `duration_timecode`
(`00:05:01:05`) — one single backwards reading at the very first push, consistent with a
stale pre-`record_start` value rather than a decode error. `RestCameraSession` now exposes
this as `clip_timecode()`, alongside `timecode()`. The conclusion is unchanged and now
doubly confirmed: `clip`, like `timecode`, is a time-based position counter and cannot show
a dropped frame either — checked directly against the same run containing a real,
operator-witnessed drop, not merely assumed from the field being formerly undecoded.

**What this means for anomaly detection**: there is no direct "a frame was dropped"
signal, and there never will be one within the swept surface — confirmed absent, not
merely undiscovered. When the camera's own policy is `Stop Recording`, the *consequence*
of a drop is already fully observable with existing code and no new library surface:
`is_recording`'s WS-driven tracking and `wait_while_recording()`'s early-return contract
caught both `Stop Recording` runs above correctly, with zero changes. When the policy is
`Alert` instead, this third run demonstrates the recording completes cleanly with
**zero** REST-observable trace across the entire event feed — consistent with (and now
much better evidenced than) the original `record_stop` finding above, where the operator
saw a warning and timecode flicker but the recording ran the full requested duration with
no early WS stop event. **The operator confirmed watching the `Alert` icon appear on the camera's own screen
during this run** — a real drop genuinely occurred, at the same combination that had just
triggered `Stop Recording` moments earlier, and it produced zero trace anywhere across the
full 46-property feed. This is no longer an inference: `Alert` mode is a confirmed,
permanent blind spot for this codebase, not an absence of a property-list entry that
happens not to have fired yet.

### `last_known_storage` / `wait_for_low_storage(...)` (Phase 8 item 1)

The one item from the Phase 8 anomaly-capture investigation that turned out to be
buildable without qualification: `/media/workingset` is a confirmed-subscribable WS
property (`docs/rest/transport.md`'s `/event/list` sweep) that was never actually
subscribed by `__aenter__` — the push channel existed on the wire and simply wasn't
consumed, the identical shape of gap `/system/format` had before the fix described above.
Confirmed live before building on it: `tools/rest/watch_events.py --host <ip> --properties
/media/workingset` (`POCKET_6K_G2 v8.6`, 2026-08-05) captured real pushes roughly once a
second during recording, body shape identical to `GET /media/workingset`'s own —
`{"size": 3, "workingset": [...]}`, same per-device fields (`activeDisk`, `clipCount`,
`deviceName`, `remainingRecordTime`, `remainingSpace`, `totalSpace`, `volume`).

`__aenter__` now subscribes `WORKINGSET_PROPERTY` alongside the other four. `_on_event`
parses every pushed body via the same `_parse_storage_state` function `storage_state()`
uses, with `active_body=None` — a pushed event carries no separate `/media/active` read,
so `active_device` resolution falls back to each device's own `activeDisk` flag rather
than `workingsetIndex`. Real event data always carried `activeDisk` correctly in every
capture so far; the fallback isn't a new code path, it's the same one `_parse_storage_state`
already uses when `/media/active` is unavailable. Result stored as
`last_known_storage: StorageState | None` — `None` until the first event arrives, never
polled, never inferred from a request the session made (design principle 4, the same
discipline `is_recording` already holds).

`wait_for_low_storage(*, min_record_time_s=None, min_space_bytes=None, timeout)` mirrors
`wait_while_recording`'s shape (an `asyncio.Event` set from `_on_event`, awaited with a
timeout) but **not its return-value polarity, deliberately** — given this codebase's own
history with `wait_while_recording`'s inverted-contract bug (above), the docstring states
the contract explicitly rather than leaving a reader to infer it by analogy: `True` means
low storage was observed (already true when called, or a threshold crossing arrived before
`timeout`); `False` means `timeout` elapsed with storage still healthy. Requires at least
one of `min_record_time_s`/`min_space_bytes` (raises `ValueError` otherwise — no silent
no-op call). No active storage device counts as low, matching the severity ordering
`_require_storage_ready()` already uses for `record_start()`. Only one threshold can be
armed per session at a time (mirrors `wait_while_recording`'s single `_recording_stopped`
event) — not intended for concurrent calls from multiple tasks on the same session, and the
armed threshold is always cleared in a `finally` block so a later unrelated push can never
be mistaken for a stale earlier call's crossing.

**Confirmed against real hardware, `POCKET_6K_G2 v8.6`, 2026-08-05** (128GB card,
`tools/rest/verify_low_storage.py`), two runs:

1. `--min-space-bytes 10000000000 --timeout 1800`: a real concurrent recording consumed
   the card from `117.57 GB` remaining down to `55.02 GB` over the full 1800s
   (~34.75 MB/s, a plausible real bitrate — `last_known_storage` tracked this continuously
   and correctly via the WS push throughout), but never crossed the 10GB threshold, so
   `wait_for_low_storage()` correctly returned `False` after the full timeout — the
   "storage stays healthy" branch, no false-positive trip.
2. `--min-space-bytes 50000000000 --timeout 300`, run immediately after (card had
   continued filling in between, now `33.96 GB` remaining, already below 50GB):
   `wait_for_low_storage()` returned `True` after **3.1s**, not instantly. `last_known_storage`
   was still `None` at the moment this call started — it was made right after connect, and
   `storage_state()`'s own `GET` immediately before never touches `last_known_storage` — so
   the immediate-return shortcut was skipped, and this genuinely waited on
   `_low_storage_event`, which the *first* real `/media/workingset` push (arriving ~3.1s
   after subscribe, consistent with this property's observed push cadence elsewhere in
   this doc) set on arrival, already below the threshold. This confirms the harder branch:
   a live crossing arriving mid-wait, driving `_check_low_storage` -> `_on_event` ->
   `_low_storage_event.set()` -> `wait_for_low_storage`'s `asyncio.wait_for` end to end on
   real hardware, not just the fake test suite.

The one sub-case still without its own dedicated real-hardware run is the immediate-return
shortcut specifically (`last_known_storage` already populated *and* already low at the
moment `wait_for_low_storage()` is called, e.g. a second call later in the same connected
session) — every run so far has called it right after connect, before `last_known_storage`
had a chance to be non-`None`. Low residual risk: the shortcut reuses the exact same
`_is_storage_low` helper run 2 crossed cleanly, and the fake test suite already exercises
the shortcut's control flow directly (`TestWaitForLowStorage`). A caller wanting to close
this specific gap would call `wait_for_low_storage()` twice in one connected session with
the second call's threshold already satisfied by the first call's own observed value, and
confirm the second call returns `True` in well under a second.

---

## Write verbs (Phases 4-5)

### `record_start()` / `record_stop()`

`PUT /transports/0/record {"recording": bool}`, verified via the dual-check design
principle 3 specifies for REST: a WS `propertyValueChanged` event on `/transports/0/record`
primary, a `GET /transports/0/record` readback secondary. `204` on the `PUT` means
accepted, not applied — if neither channel confirms the requested state within the
overall budget, `BMDVerificationError` is raised.

**`record_start()` and `record_stop()` use different overall budgets.** The primary WS
wait always gets exactly `verify_timeout_s` (constructor parameter, default `5.0`,
distinct from `timeout_s`'s single-request timeout and `ws_timeout_s`'s WS connect
timeout) for both. Where they differ is the secondary readback: `record_start`'s overall
budget equals `verify_timeout_s`, so — as before — it gets a single `GET` after the
primary wait, no retry. `record_stop`'s overall budget is `stop_verify_timeout_s`
(constructor parameter, defaults to `verify_timeout_s * 3` — `15.0`s at the library
default), and its secondary `GET` is **polled** every `RECORD_POLL_INTERVAL_S` (`0.5`s)
for whatever's left of that wider budget after the primary wait, rather than fired once.
See the real-hardware finding below for why `record_stop` needed this and
`record_start` didn't.

`record_start()` first calls `storage_state()` and raises `BMDStorageError` — design
principle 10's REST implementation for the record write path — when there is no active
storage device, or **the active device reports `remaining_space <= 0`**. `record_stop()`
carries no such check and is a no-op when `is_recording` already positively confirms the
camera isn't recording, mirroring the BLE `CameraSession.record_stop`'s documented
no-echo-on-redundant-write handling (`docs/ble/recording.md`) — whether this camera's REST
endpoint behaves the same way on a redundant `PUT` is unconfirmed, but the guard is
harmless either way since `is_recording` is only ever notification-derived (design
principle 4), never assumed.

**Gate changed from `remaining_record_time` to `remaining_space`, Phase 9.** The original
version of this check gated on `remaining_record_time <= 0` instead. The real-hardware
finding above (`examples/rest_record_test_clip.py`, three runs, `POCKET_6K_G2 v8.6`,
2026-08-05) confirmed `remaining_record_time` is stale immediately after a
`set_camera_format()` switch — it keeps reporting the *pre-switch* format's estimate until
a recording actually starts — while `remaining_space` stayed accurate throughout every run
to date, including immediately after a switch. Gating a pre-flight check on the one field
confirmed unreliable in exactly the window this check runs risked either a false pass (a
large stale estimate masking a real shortage under the new format) or a false block (a
small stale estimate blocking a start the new format could actually satisfy) — which
direction depends on which way the switch's bitrate changed, and no run to date happened to
land in either failure mode, only the "stale but still comfortably positive" case documented
above. **Stated honestly, not overclaimed**: no real-hardware run has ever directly observed
`remaining_record_time` actually hit `<= 0` post-switch — only staleness in one direction.
Rather than build format-switch-tracking machinery to detect staleness, this check now
trusts the field that has never been observed stale in any run: real bytes free.
`remaining_record_time` remains available via `storage_state()` for informational use; it
just no longer gates this precondition.

**Real-hardware-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-13** (`examples/rest_record_test_clip.py`,
the ported version): a fourth run reconfirmed the staleness finding in the exact same shape —
`remaining_record_time` read `61245`s both immediately before and immediately after the
`set_camera_format()` switch, only updating to `12401`s once recording actually started —
while `record_start()` itself succeeded cleanly under the new `remaining_space`-only gate.
This run's `remaining_record_time` stayed comfortably positive throughout (never near `0`),
so — like every run before it — it doesn't directly prove the old gate would have blocked a
start the new one correctly allows; that specific failure mode remains real-hardware-
unobserved. What this run does newly confirm is the gate change causing no regression: a real
recording started, held for its full duration, and stopped cleanly with the reworked
precondition in place.

**Capability check is GET-only, deliberately.** Both methods refuse to run unless the
target camera/firmware's `rest/<fw>.json` profile confirms `/transports/0/record`'s `GET`
side was swept (design principle 7) — but `tools/rest/probe_endpoints.py --probe-writes`
puts this path on its `NEVER_WRITE` list (a same-value `PUT` here would still start or stop
a real recording, unlike the idempotent probe every other write-probeable endpoint gets).
So no profile will ever carry a confirmed `put_supported` for this path, and the capability
check does not attempt one — the `PUT` itself is confirmed by this method's own
verification, on every call, the same way BLE's `record_start`/`record_stop` are verified
without a profile-level "this command works" flag.

**`is_recording` is never set directly by either method** — consistent with design
principle 4's rule that it updates only from a `propertyValueChanged` event, never from a
request the session itself made. When the primary WS event confirms, `is_recording` is
already current by the time `record_start`/`record_stop` returns (the same event routes
through `_on_event` independently). When only the secondary `GET` readback confirms
(no event arrived in time), `record_start`/`record_stop` still returns successfully, but
`is_recording` can remain stale/`None` until a real event eventually arrives — the same
gap `is_recording`'s own docstring already describes for the read path, not a new one this
introduces.

**Real-hardware-confirmed, `POCKET_6K_G2` and `POCKET_6K_PRO v8.6`, 2026-08-03**:
`examples/rest_record_start_stop.py`, 3 cycles per camera. `PUT /transports/0/record` is
implemented — `record_start` and `record_stop` were each confirmed 6/6 (3/3 per camera) via
the dual-check, closing the "Open" row `docs/rest/transport.md`'s sweep results left for
this endpoint (`NEVER_WRITE` there by design — this session's own verification was always
going to be the only channel this endpoint ever got). This run is also what surfaced
`wait_while_recording`'s return-value-inversion defect described above — the two findings
came from the same run: the writes themselves were solid 6/6, only the surrounding
hold-and-check helper had the bug.

### `confirm_new_clip(clips_before, storage_before=None)` → `RecordingResult` (Phase 9)

Formalizes the before/after `clips()` diff `examples/rest_record_test_clip.py` did by hand
across its three real-hardware runs into a reusable method — identifies the clip a
`record_start()`/`record_stop()` cycle just wrote, and (optionally) how many bytes it used.

**Why `clips_before` must be caller-supplied, not automatic.** Unlike everything on
`CameraState`, `GET /clips/list` has no WS event of any kind — this cannot be
notification-driven (design principle 4 has nothing to observe here), and a `Clip` carries
no "just written" flag either (design principle 9: reads are best-effort, not proof of
anything not directly reported). A before-snapshot the caller already took — via `clips()`,
before calling `record_start()` — is the only way to name which clip is new.

**Precondition, stated loudly rather than left implicit: same connected session only.**
`clip_unique_id` is real-hardware-confirmed *not* stable across reconnects (see `clips()`'s
own section: the same two physical files reported `clipUniqueId` `15`/`16` in one session
and `1`/`2` in a later one, minutes apart, no reformat or recording in between). A
`clips_before` snapshot captured in a *different* session than the one `confirm_new_clip()`
runs in can produce a silently wrong "new clip" or a false ambiguity — this only means what
it claims within one `async with RestCameraSession(...)` block.

Diffs `clips_before` against a fresh `clips()` read by `clip_unique_id`. Raises
`BMDVerificationError` if zero new clips are found (the recording never happened, or hasn't
been indexed yet), and **also** if more than one new clip appears — this method does not
guess which one is "the" recording, matching this codebase's established refusal to guess
under ambiguity (`_resolve_supported_format`'s `sensor_resolution` ambiguity is the closest
precedent). `BMDVerificationError` rather than `BMDUnsupportedError` for both cases: this is
a verification question (can this session confirm what it wrote), not a capability question
(does the camera support something). **Honestly unexercised**: no real-hardware run in this
codebase's history has ever produced more than one new clip from a single
`record_start`/`record_stop` cycle — the >1 branch is defensive, not confirmed-necessary.

If `storage_before` is given and both it and a fresh `storage_state()` report an
`active_device`, `RecordingResult.bytes_written` is `storage_before.active_device.
remaining_space` minus the fresh reading's — the same computation
`examples/rest_record_test_clip.py` did by hand, since `Clip` has no size field of its own
(the same gap Phase 6's `rest/media.py` hit for stills). `None` if `storage_before` is
omitted or either snapshot has no active device — never a guessed or defaulted number.

**Real behavior change from the manual version it replaces, not a pure refactor.** The old
hand-written loop in `rest_record_test_clip.py` silently tolerated any number of "new"
clips it found; `confirm_new_clip()` raises on both zero and more than one. Unit-tested
(`TestConfirmNewClip`); the ported example script is the real-hardware re-verification
harness — the manual-diff version already ran clean 3x, so a clean rerun against
`confirm_new_clip()` confirms parity.

**Real-hardware-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-13** (`examples/rest_record_test_clip.py`,
`BRAW 5:1 4K DCI 23.98`, 600s, 1TB card): clean end to end. `confirm_new_clip(clips_before)`
correctly identified `clip_unique_id=20` (the only new id — clip count went `19` -> `20`) out
of a real 19-clip-then-20-clip card, and `bytes_written` was computed as `35390881792` bytes
(`842542612480 - 807151730688`, exact) — the first real-hardware confirmation of the positive
path (exactly one new clip, both storage snapshots carrying an `active_device`). `record_stop`
did not raise this run — the first real-hardware run of `record_stop` since
`stop_verify_timeout_s` was added (the three runs that motivated the fix all predate it). This
run always had a real active device on both sides, so it alone didn't exercise the zero-new-clip,
more-than-one-new-clip, or `bytes_written`'s `None`-returning branches.

**Four of those five branches closed, `POCKET_6K_G2 v8.6`, 2026-08-24** —
`tools/rest/verify_confirm_new_clip_edge_cases.py`, first run, all tests passed: zero new clips
(no recording, a same-instant snapshot correctly found nothing new), `bytes_written=None` with
`storage_before` omitted, `bytes_written=None` with a hand-built no-active-device
`StorageState` passed as `storage_before` (legitimate — the method only inspects the shape of
what it's given, never re-validates it was freshly fetched), and more-than-one-new-clip (two
real clips recorded back-to-back sharing one before-snapshot, both ids named correctly in the
raised error). **One branch remains genuinely real-hardware-unexercised, deliberately**:
`bytes_written` staying `None` because the *live* second `storage_state()` call finds no
active device — forcing it would need the SD card to report no active device at the exact
moment right after a clip was written, and pulling the card would very likely fail the
*first* `clips()` call instead of ever reaching this branch. See
`tools/rest/verify_confirm_new_clip_edge_cases.py`'s own module docstring and
`docs/rest/transport.md`'s matching section for the full reasoning and run write-up.

### `set_camera_format(codec, variant, resolution, fps)`

`PUT /system/format`, carrying `codec`/`frameRate`/`recordResolution` together — the REST
analogue of BLE `CameraSession.set_camera_format`, but one request where BLE needs up to
three packets (`set_video_format`/`set_codec_quality`/`set_recording_format`) and a
two-step proxy workaround for the one combination its `dimension_enum` table can't reach
directly. `RestCameraSession.set_camera_format` needs neither: `/system/format` carries
codec, resolution, and frame rate together, so there's nothing to sequence.

Takes the same profile vocabulary a BLE script already uses (`"BRAW"`/`"5:1"`, `"4K DCI"`,
`"23.98"`), translated to REST's own spelling via "Codec name mapping" below —
`resolution`/`fps` need no translation at all (see that section). Local validation first
(`require_codec`/`require_resolution`/`require_fps_mode`, shared with BLE — design
principle 1), raising `ValueError` for a name the profile doesn't know. **None of BLE's
`dimension_enums`/`m_rate`/`frame_flags`/`known_unreachable`/`max_fps_int` are consulted
here** — those stay BLE-only (`CameraSession` still needs them). Instead, the requested
`(codec, resolution, fps)` combination is checked against the camera's own **live**
`supported_formats()` capability matrix before any write — design principle 7's REST
sibling to BLE's static ceiling checks — raising `BMDUnsupportedError` immediately when the
camera doesn't report offering it.

**Merge-before-write, not a partial body.** `GET /system/format` first, then only
`codec`/`frameRate`/`recordResolution`/`sensorResolution` are overwritten in that same body
before the `PUT` — never a hand-built partial one — because a partial body risks resetting
`offSpeedEnabled`/`offSpeedFrameRate`/min/max off-speed to defaults, which are the only
fields still genuinely preserved unchanged from the pre-write `GET`.

**`sensorResolution` is derived from the matched `supported_formats()` entry, not
preserved.** Real-hardware-confirmed defect, `POCKET_6K_G2 v8.6`, 2026-08-03 (see "Real
hardware" below): `GET /system/supportedFormats` pairs the same `4096×2160`
`recordResolution` with **different** `sensorResolution` per codec —
`ProRes` → `5744×3024`, `BRaw` → `4096×2160`. The first version of this method preserved
whatever `sensorResolution` the *current* format already had, which is only correct when a
write doesn't cross that pairing — a `ProRes/4K DCI` write followed by `BRAW/4K DCI` sent
`BRaw`'s codec/resolution/fps together with `ProRes`'s stale `sensorResolution`, an
internally inconsistent body the camera correctly rejected with
`400 {"error": "Format is not supported"}` (not a `501`/`BMDUnsupportedError`, since the
requested combination itself was genuinely supported). Fixed by deriving
`sensorResolution` from whichever `supported_formats()` entry matched the requested
`(codec, recordResolution, fps)` — the exact pairing the camera itself declared — instead
of leaving it untouched. If more than one entry matches with *different*
`sensorResolution` values (a real case: `ProRes` at `1920×1080` pairs with three different
sensor resolutions), the method raises `BMDUnsupportedError` naming the ambiguity rather
than guessing — see `_resolve_supported_format`'s docstring — *unless* the optional
`sensor_resolution` parameter disambiguates it explicitly (only among values the camera
itself already pairs with the requested combination; it cannot ask for one the camera
doesn't offer there). `tools/rest/sweep_camera_format.py` is what that parameter exists
for — see "`tools/rest/sweep_camera_format.py`" below. This is **not** general-purpose
`sensorResolution` selection (still out of scope, see below) — it's making sure every
write this method already attempts is internally consistent with what the camera itself
reports pairing together.

Verified via the same dual-check pattern as `record_start`/`record_stop`: a WS
`propertyValueChanged` event on `/system/format` primary, a `GET /system/format` readback
secondary, `BMDVerificationError` if neither confirms the requested
`codec`/`frameRate`/`recordResolution`/`sensorResolution` within `verify_timeout_s`.

**Capability check is put_supported-gated, unlike `record_start`/`record_stop`.**
`/system/format`'s `PUT` *was* write-probed (`tools/rest/probe_endpoints.py --probe-writes`
— it's not on the `NEVER_WRITE` list, since a same-value `PUT` here is genuinely
idempotent), so both `payloads/models/POCKET_6K_G2/rest/v8.6.json` and
`.../POCKET_6K_PRO/rest/v8.6.json` carry a real `put_supported: true` for this path
(`docs/rest/transport.md`'s "**`PUT /system/format` returns `204`**" finding). This method
raises `BMDUnsupportedError` immediately unless that flag is confirmed — the more direct
design principle 7 check `record_start`/`record_stop` couldn't use, since their endpoint
was structurally never write-probed.

**Real hardware, `POCKET_6K_G2 v8.6`, 2026-08-03** (`examples/rest_change_format.py`'s
first run): step 1, `ProRes/422/4K DCI @ 23.98`, **confirmed** — exactly the combination
BLE's `settings.md` records as `known_unreachable` after nine falsification attempts,
now reached over REST on the first attempt. Step 2, `BRAW/5:1/4K DCI @ 23.98`, run
immediately after, **failed with a real `400`** — the `sensorResolution` defect described
above, not a codec-name or capability-check problem (the capability check had already
passed; the rejection came from the camera itself, on the `PUT`).

**Fix re-confirmed, both cameras, 2026-08-03** (`examples/rest_change_format.py`, re-run
after the `sensorResolution` fix): both steps — `ProRes/422/4K DCI @ 23.98` then
`BRAW/5:1/4K DCI @ 23.98` — confirmed cleanly, back to back, on both `POCKET_6K_G2 v8.6`
and `POCKET_6K_PRO v8.6`. The full round trip this section's earlier note called "still
open" is now closed on both cameras this codebase targets.

### `tools/rest/sweep_camera_format.py` — exhaustive verification sweep

The REST analogue of `tools/control/sweep_camera_format.py`: runs `set_camera_format()`,
the real production API, across every `(codec, variant, resolution, fps)` combination a
profile's `codecs`/`resolutions`/`fps_modes` tables claim, in one connected session, and
reports which combinations actually confirm. Built directly in response to the
`sensorResolution` defect above — two manual calls in a row were enough to find a real bug
nothing in this codebase's tooling would have caught systematically beforehand.

**Adds a dimension BLE's sweep tool never needed: sensor area.** `GET
/system/supportedFormats` can pair the *same* `(codec, recordResolution, fps)` with more
than one `sensorResolution` (confirmed: `ProRes` at `1920×1080` pairs with three —
`docs/rest/transport.md`). `set_camera_format()` gained an optional `sensor_resolution`
parameter specifically so this tool can exercise every one of those pairings explicitly —
without it, `set_camera_format()` refuses an ambiguous combination
(`BMDUnsupportedError`) rather than guessing, and the ambiguous ones would never get
swept at all. One live `GET /system/supportedFormats` read, taken right after connecting,
expands every profile-table combination into one sweep item per distinct
`sensorResolution` the camera actually pairs with it — or a single `"unsupported"` item
with no write attempted when the camera doesn't offer the combination at all.

**Deliberately sweeps `known_unreachable`/`max_fps_int` combinations BLE's tool
excludes by default** — those are BLE-write-path-specific concepts REST's live capability
check makes unnecessary (see `set_camera_format`'s docstring), and re-testing them is
exactly how `ProRes/4K DCI` was already confirmed reachable over REST
(`docs/ble/settings.md` §16.2). No filters analogous to BLE's
`--include-known-unreachable`/`--include-unsupported-fps` exist here — REST sweeps
everything the profile tables claim by default.

`--dry-run` connects (read-only — one `supported_formats()` call) rather than staying
fully offline like the BLE tool's, since an offline count would undercount every
ambiguous combination before expansion.

**Real hardware, `POCKET_6K_G2 v8.6`, 2026-08-03** (full sweep, no filters): **544
confirmed, 16 unsupported (the `BRAW`/`6K` `59.94`/`60` combinations the camera's own
`supportedFormats` doesn't offer — a real hardware ceiling, not a bug), 0 missing profile
data, 0 unconfirmed.** Every requested `(codec, variant, resolution, fps, sensor
resolution)` combination the camera claims to support was confirmed — the strongest single
piece of evidence yet that `set_camera_format`'s write path, including the
`sensorResolution` derivation fix above, is solid across the *entire* combination space on
this camera, not just the two combinations manually exercised so far.

**But this same run also caught a second real defect**, alongside the correctness result
above: every one of the 544 confirmed writes took almost exactly `verify_timeout_s`
(6.0s–6.1s each, not the fast primary-channel case) — a uniform timing signature that
would be a strange coincidence if the primary WS-event channel were ever actually winning
the race. It wasn't: `__aenter__` subscribed the router to `/transports/0/record` but never
to `/system/format`, so nothing had ever told the camera to push `/system/format` events to
this connection at all — `set_camera_format`'s `arm()`/`wait_for()` on it was waiting on a
channel that could structurally never deliver. Every confirmation in this run came from the
secondary `GET` readback, after burning the full primary timeout uselessly first. Fixed by
subscribing to `/system/format` in `__aenter__` too (see "Connection lifecycle" above).

**Fix re-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-04**: an identical full re-sweep — same
544 confirmed, 16 unsupported, 0 unconfirmed — but per-combination timing dropped to
0.0–0.2s (a handful of 1.1s outliers), not the uniform ~6.0s the pre-fix run showed. The
primary WS-event channel is genuinely winning the race now, not just theoretically fixed.

---

## Write verbs (Phase 7 — playback and gallery)

Entirely new capability BLE never reached (`docs/rest/transport.md`'s "New capability
REST brings"). Built to the same dual-check discipline as every other write in this
session, though it started from a materially weaker evidentiary base than Phases 4/5 had
going in — see each method's own docstring for exactly what is and isn't confirmed. The
timeline write was debugged directly via Postman across four rounds, disproving its
original `set_timeline(clip_unique_ids: list[int])` design outright and replacing it with
`select_clip(clip_unique_id: int)` — see its own section below for the full trail. The
entire sequence — `select_clip()` through `exit_playback()` — is now real-hardware-
confirmed end to end on both `POCKET_6K_G2` and `POCKET_6K_PRO v8.6` (2026-08-04,
`examples/rest_playback.py`).

### `timeline_clip_ids()` → `tuple[int, ...]`

The one read verb in this section — a plain `GET /timelines/0`, parsed by the same
`_parse_timeline_clip_ids()` `select_clip()`'s own poll uses, but standalone: no format
switch, no `select_clip()`-style "is my clip a member" check, just whatever the camera
currently reports. Added specifically so a caller (or a diagnostic script) can inspect the
timeline independently of driving it — `select_clip()`'s own poll only ever checks for the
*requested* clip's membership and would not itself notice an unrelated leftover entry.
First consumer: `examples/check_timeline_stale_entries.py` (below), built to test whether
skipping `DELETE /timelines/0` (`select_clip()`'s finding #1 — `501` on this firmware)
ever leaves a previous format's clips mixed in with a newly-selected format's clips.

**Answered, `POCKET_6K_G2 v8.6`, 2026-08-04:** no. Two runs, in opposite directions
(clip A `ProRes:Proxy @ 4096x2160p23.98` -> clip B `BRaw:8_1 @ 6144x2560p23.98`, then the
reverse), each read the timeline right after switching. Both times the readback contained
*only* the just-selected clip's own format group — no trace of the previous format's
clips. `POST /timelines/0/add` fully replaces the timeline's contents on this firmware,
even though `DELETE` never runs.

**Caveat added 2026-08-05, `select_clip()`'s finding #7 below:** both runs above switched
the camera's format (to clip B's, then back to clip A's) before `POST`ing. Since `POST
/timelines/0/add` is now known to be a no-op when the target clip's format doesn't already
match, these two runs can't distinguish "the `POST` replaced the timeline's contents" from
"the format switch alone already had, and the `POST` never mattered." The "no stale
entries" observation itself still stands; the causal reading of *why* does not.
**Closed, same day, `tools/rest/diagnose_timeline.py --skip-post`:** a format switch with
zero `DELETE`/`POST` sent populated the timeline correctly on its own — see finding #7's
own write-up below for the decisive run.

### `select_clip(clip_unique_id)`

Makes one clip (`Clip.clip_unique_id`, from `clips()`, Phase 3) playable: switches the
camera's format to match that clip's own recorded format if it doesn't already
(`set_camera_format()`, Phase 5), then `DELETE /timelines/0` (clears whatever timeline
exists) + one `POST /timelines/0/add` to sync the camera's playback timeline. Required
before `enter_playback()`/`play()` can show anything.

**Replaces an earlier `set_timeline(clip_unique_ids: list[int])` design that real
hardware disproved outright.** That design assumed a caller could hand-curate an
arbitrary ordered subset of clips into a custom playlist — a reasonable reading of the
migration plan, and the only design anyone could have written before real evidence
existed. Four rounds of real-hardware testing said otherwise:

**The one Phase 7 write with zero prior sweep evidence of any kind.** `/timelines/0`
(DELETE) and `/timelines/0/add` (POST) are both in `tools/rest/probe_endpoints.py`'s
`NEVER_WRITE` list — a delete or add is destructive regardless of value, so even the
idempotent same-value-probe method that confirmed `/transports/0`/`/transports/0/playback`
writable can't touch them. This is the same position `/transports/0/record` was in before
Phase 4's own writes were the first real evidence either way.

**Real-hardware finding #1, `POCKET_6K_G2 v8.6`, 2026-08-04 (Phase 7's first run):**
`DELETE /timelines/0` returned `501` — the DELETE *method* on this path is genuinely not
implemented on this firmware, a different and more specific answer than the read sweep's
confirmed `GET`. Since a `BMDUnsupportedError` propagating from the DELETE would have
blocked every subsequent `POST` from ever being attempted, and whether this camera even
needs an explicit clear before adding is itself unconfirmed, `select_clip()` catches a
`501` from the DELETE specifically, logs it, and proceeds straight to `POST`ing — any other
error from the DELETE still propagates normally.

**Real-hardware finding #2, same run, immediately after:** the original `POST` body — one
request per clip, each a bare `{"clipUniqueId": id}` reusing `/clips/list`'s field name on
the hypothesis the two endpoints share a vocabulary — was rejected: `400
{"error": "Invalid clips data"}`. The error names `"clips"` (plural) specifically, which
reads as "the body needs a top-level `clips` key", not "the field name inside it is
wrong". `_parse_timeline_clip_ids()` already established that `GET /timelines/0`'s own
confirmed response carries exactly that shape — `{"clips": [{"clipUniqueId": ...}, ...]}`
— so the fix sends the same shape back: `POST /timelines/0/add` carrying the requested
clip under `"clips"`, instead of a bare-object body.

**Real-hardware finding #3, `POCKET_6K_PRO v8.6`, 2026-08-04, isolated via direct Postman
requests (bypassing this session entirely):** finding #2's `{"clips":
[{"clipUniqueId": id}]}` body is confirmed the *only* accepted shape — `{"clips":
[id]}` and `{"clipUniqueIds": [id]}` both come back `{"error": "Not implemented for this
device"}`, `{"clip": {"clipUniqueId": id}}` reproduces finding #2's `400`, and
`PUT /timelines/0` (tried as a hoped-for full-replace alternative to the broken `DELETE`)
is flatly `405 Method Not Allowed`. But the confirmed shape's `204` turned out to be a
trap: `POST`ing `{"clips": [{"clipUniqueId": 1}]}` against a timeline that already held a
*different* clip (id `12`) returned `204` — accepted — and a `GET /timelines/0`
immediately after still reported only `[12]`, completely unchanged.

**Real-hardware finding #4, same session, the decisive one:** clip `1` was `ProRes:Proxy`
at `4096x2160p24`; clip `12`, the one already resident, was `BRaw:Q3` at `6144x2560p60` —
so finding #3 looked at first like the same format-precondition already known from
`enter_playback()` (see below), just enforced one step earlier, at *timeline build* time.
The operator then switched the camera's format to `ProRes:Proxy @ 4096x2160p24`
(confirmed via `GET /system/format` immediately before) and re-`POST`ed
`{"clips": [{"clipUniqueId": 1}]}`. The result wasn't `[1]` — it was **seven** clips,
`[10, 1, 9, 8, 7, 5, 6]`, every clip on the card sharing that exact format. Repeating the
identical request with `clipUniqueId: 5` in place of `1`, format re-verified unchanged, no
camera-body interaction in between, produced the *identical* seven-clip set. Independently,
the camera's own on-screen playback view (photographed live) showed `"CLIP 1/7"` for that
same group, confirming this is native camera behavior, not a REST quirk. **Conclusion: the
camera has no concept of a caller-curated playlist. The timeline is always every clip
matching the camera's current format; the requested `clipUniqueId` does not select which
clips end up in it.**

That conclusion is why `set_timeline(clip_unique_ids: list[int])` was retired rather than
patched again. `select_clip(clip_unique_id)` accepts the real contract: pick one clip, let
its format define the entire resulting group (switching format first if needed — the same
precondition `enter_playback()` documents, now handled automatically instead of left to
the caller), and verify the requested clip is a *member* of whatever `GET /timelines/0`
reports, not that it's the sole entry. `resolve_ble_codec_name` (`mapping.py`) turns the
clip's REST-reported codec (e.g. `"ProRes:Proxy"`) back into the `(family, variant)`
`set_camera_format()` needs by scanning the profile's confirmed `format_names` table —
deliberately no derivation fallback, since `mapping.py`'s own docstring already establishes
that guessing the REST->BLE direction isn't safe (ProRes's `"422"` -> `"Original"` is the
proof). `_resolution_name_for_dimensions` does the same for the clip's pixel dimensions
against the profile's `resolutions` table. Either returning nothing raises
`BMDUnsupportedError` naming the gap rather than guessing.

**Real-hardware finding #5, `POCKET_6K_G2` and `POCKET_6K_PRO v8.6`, 2026-08-04, the first
run of `select_clip()` itself:** `python examples/rest_playback.py` composed the whole
combination — format check, `DELETE`-then-`POST` timeline sync, membership poll — and it
ran clean on both cameras, this method's own dual-check passing each time. The tested
clip's format already matched the camera's current setting on both runs, so this
specific evidence doesn't cover the `set_camera_format()` branch inside `select_clip()`
(that piece's own evidence is still Phase 5's, not new here) — a clip whose format
*doesn't* match, forcing the switch, remains the next real-hardware gap to close.

**Real-hardware finding #6, `POCKET_6K_G2 v8.6`, 2026-08-04, closing finding #1's last
open question:** `examples/check_timeline_stale_entries.py` switched between two clips of
different formats via `select_clip()` — `ProRes:Proxy @ 4096x2160p23.98` and
`BRaw:8_1 @ 6144x2560p23.98` — checking `timeline_clip_ids()` after each switch, in both
directions (A -> B, then B -> A). Both runs: the readback after the second switch
contained *only* the newly-selected clip's own format group, nothing from the one before
it. **Skipping the `DELETE` clear does not leave stale cross-format entries** — `POST
/timelines/0/add` fully replaces the timeline's contents on this firmware regardless.
*(Caveat added 2026-08-05 — finding #7 below: both runs here switched format before
`POST`ing, so this can't distinguish the `POST` doing the replacing from the format switch
alone already having done it. The "no stale entries" result stands regardless. **Closed,
same day**: `tools/rest/diagnose_timeline.py --skip-post` confirmed the format switch alone
does the work — see finding #7's write-up below.)*

No WS event shape is known for `/timelines/0`, so verification there isn't the usual
primary/secondary dual-check — it's a poll: `GET /timelines/0` repeatedly (default every
0.5s, `poll_interval_s`) until `_parse_timeline_clip_ids()`'s reading *contains*
`clip_unique_id` (membership, not exact-list equality — the real result is never just the
one clip requested), or `verify_timeout_s` elapses (`BMDVerificationError`).
`_parse_timeline_clip_ids()`'s shape is real-hardware-confirmed (same Postman session):
`{"clips": [{"clipUniqueId": int, "frameCount": int}]}` — a dict-list under `"clips"`, the
extra `"frameCount"` field simply ignored. The flat-int-list branch the parser also accepts
predates this confirmation and has never actually been observed; it's kept only as a
defensive fallback.

**Format-switch logging gap, found immediately after the sixth finding above.** A
follow-up run against a genuinely mismatched clip played back correctly, but nothing in
the console indicated a `PUT /system/format` had happened — `RestClient`'s own
`GET`/`PUT`/`DELETE`/`POST` logging (`client.py`) is `DEBUG`-only, below
`examples/rest_playback.py`'s `logging.basicConfig(level=logging.INFO)`, and neither
`select_clip()` nor `set_camera_format()` logged anything of their own at `INFO` to fill
the gap. Fixed: `select_clip()` now logs at `INFO` when it detects a mismatch (naming both
the clip's format and the camera's current one) before calling `set_camera_format()`,
which itself now logs the requested `codec`/`variant`/`resolution`/`fps` right before its
own `PUT`. Silence at `INFO` during `select_clip()` now reliably means the format already
matched, per design principle for logging ("Operation boundaries" at `INFO`) that this
path had been missing.

**Failure, `POCKET_6K_G2 v8.6`, 2026-08-05, on a freshly-reformatted 128GB card.** Two
back-to-back runs of `examples/rest_playback.py` both failed identically at
`select_clip()`: no format switch was attempted (the camera's current format already
matched `clips()[0]`'s own, per the log), `DELETE` returned the expected `501`, `POST
/timelines/0/add {"clips": [{"clipUniqueId": 1}]}` raised no error — but `GET
/timelines/0` came back genuinely empty (`{"clips": []}`) on every read within the 5s poll
budget, not "other clips but not mine". That specific card and clip no longer exist (the
card has since been reformatted again), so the exact original cause can't be
retroactively confirmed — but `tools/rest/diagnose_timeline.py`'s first run, below,
uncovered the general mechanism this failure is almost certainly an instance of.

**Real-hardware finding #7, `POCKET_6K_G2 v8.6`, 2026-08-05, `tools/rest/diagnose_timeline.py`
(built for the failure above), the mechanism itself.** Two clips on a reformatted card:
`clip_unique_id=15` (`ProRes:HQ @ 1920x1080p25`), `clip_unique_id=16` (`BRaw:3_1 @
2880x1512p25`). Camera's live format at the time: `BRaw:3_1 @ 2880x1512p25` — exactly
clip 16's own. `GET /timelines/0` *before this run made any write at all* already read
`(16,)`. This tool, unlike `select_clip()`, deliberately makes no format-matching check
and attempts no switch — it targeted clip 15 directly, `POST`ing
`{"clips": [{"clipUniqueId": 15}]}` against a camera still sitting at clip 16's format.
The `POST` raised no error, but polled every 1s for the full 30s: `(16,)`, unchanged,
every single read — clip 15 never appeared.

**`POST /timelines/0/add` is a no-op when the requested clip's format does not match the
camera's live current format — it does not raise, does not change the timeline, and does
not eventually catch up given more time.** This refines finding #4's conclusion rather
than replacing it: the timeline is not "every clip matching the *requested* clip's own
format" so much as it is *continuously* every clip matching the camera's *live* format,
`POST` or no `POST` — the pre-write baseline in this same run already showed `(16,)` with
no POST behind it in this session at all. Every one of findings #1-#6's confirmed
successes switched the camera's format to match the target clip *before* `POST`ing (via
`set_camera_format()`, either by `select_clip()` itself or by the operator manually) — so
none of them, on their own, distinguish "the `POST` did the selecting" from "the format
switch alone was already enough, and the `POST` never mattered." This diagnostic's
negative result is the first evidence pointing at the second reading.

**Closed, `POCKET_6K_G2 v8.6`, 2026-08-05, `tools/rest/diagnose_timeline.py --skip-post`
(added the same day for exactly this test): the format switch alone populates the
timeline — `POST /timelines/0/add` has never been shown to do anything on this firmware.**
First attempt targeted the default clip (`clips()[0]`, `clip_unique_id=1`,
`ProRes:HQ @ 1920x1080p25`) and hit the already-known sensor-resolution ambiguity
(`set_camera_format` refusing to guess among 3 candidate `sensorResolution` values — this
tool, unlike `examples/rest_playback.py`, doesn't compose the retry) — not a new finding,
the same gap noted below. The operator then deleted that clip from the card to route
around it (not a bug workaround in this codebase, an operator action on the camera itself),
leaving one clip, `clip_unique_id=2` (`BRaw:3_1 @ 2880x1512p25`). Re-run: `get_format()`
before any write showed a genuinely mismatched `ProRes:HQ @ 3840x2160p25`,
`GET /timelines/0` before any write was empty (`()`), `set_camera_format('BRAW', '3:1',
'2.8K 17:9', '25')` confirmed with **zero `DELETE` and zero `POST` sent**, and
`GET /timelines/0` immediately after — still no `POST` anywhere in this session — already
read `(2,)`. The format switch by itself is what populates the timeline; `POST
/timelines/0/add` is confirmed dead weight for the single-matching-clip case tested here.
`select_clip()` still sends the `POST` (harmless — a `204` either way) but nothing in any
run to date, including this one, has shown it doing real work. **One case this run doesn't
cover**: finding #4's seven-clips-share-a-format scenario — whether the format switch alone
surfaces *every* matching clip, or only some, when more than one shares the target format,
remains untested with `POST` fully absent; every multi-clip observation on record
(finding #4, and `check_timeline_stale_entries.py`'s both directions) had a `POST`
in the sequence.

**Immediately available next test, same card**: `clip_unique_id=15` is now a
real, confirmed-mismatched clip sitting on the card — exactly the case finding #5 flagged
as untested (`select_clip()`'s own `set_camera_format()` branch, forced by a genuine
mismatch). Since `clips()[0]` is clip 15 on this card, a plain run of
`examples/rest_playback.py` right now should exercise it: `select_clip()`'s comparison
(`'ProRes:HQ' != 'BRaw:3_1'`, unambiguous, no shared-spelling edge case) should detect the
mismatch, log it at `INFO` (the fix above), call `set_camera_format()`, and only then
`POST`. If that succeeds, it closes finding #5 and confirms `select_clip()`'s own
switch-then-sync sequence handles the exact scenario this diagnostic just proved matters —
not yet run.

### `enter_playback()` / `exit_playback()`

`PUT /transports/0 {"mode": "Output"}` / `{"mode": "InputPreview"}`, via the shared
`_set_transport_mode()` helper — the same dual-check shape as `_set_recording_state()`: a
WS `propertyValueChanged` event on `/transports/0` primary, a `GET` readback secondary,
parsed by `_transport_mode()`.

This is Phase 7's best-evidenced write. `/transports/0`'s `PUT` returned a real `204` on a
same-value probe on both cameras (`docs/rest/transport.md`), and the reshaping table for
that probe — "Echoes `InputPreview`/`Output`; skips when the camera reports `InputRecord`"
— is itself confirmation that a real `GET /transports/0` body looks like
`{"mode": "InputPreview"|"InputRecord"|"Output"}`, not a shape invented for this session.
`"InputRecord"` is read-only here (set through `/transports/0/record` instead); this method
doesn't special-case that itself, since the camera's own rejection of an invalid mode is
already the actual guard — it just surfaces as a failed verification rather than a
`ValueError`.

**Format precondition, confirmed real on the camera body (`POCKET_6K_PRO v8.6`,
2026-08-04):** a clip only plays if the camera's *current* codec/quality/resolution/fps
matches the format the clip was recorded with — real-hardware testing showed this isn't
just a playback-time restriction but the very thing that defines the camera's entire
playable timeline (`select_clip()`'s section above has the full trail: every clip sharing
the current format, and nothing else). `select_clip()` now handles this automatically —
`Clip.codec`/`Clip.video_format` (Phase 3) are mapped back onto `set_camera_format()`'s
vocabulary via `resolve_ble_codec_name`/`_resolution_name_for_dimensions`, and the format
is switched before the timeline is synced — so a caller going through `select_clip()`
doesn't need to think about this directly. Calling `enter_playback()`/`play()`/`shuttle()`
without having gone through `select_clip()` first, or with the camera's format having
changed since, is expected to dual-check-fail with a `BMDVerificationError` rather than a
clearer diagnosis naming the mismatch.

**Confirmed: `exit_playback()` reverts the camera's format to whatever it was before
playback mode was entered — `POCKET_6K_G2 v8.6`, 2026-08-04, isolated across three
real-hardware runs the same day.** `examples/rest_playback.py` brackets its run with `GET
/system/format` snapshots (`_print_format`). A `select_clip()` call that switched the
camera's format (e.g. `ProRes:HQ @ 4096x2160p25` -> the requested clip's own `BRaw:5_1 @
6144x3456p25`) was followed by three progressively-truncated sequences, each checking the
format immediately after the last call made:

1. `select_clip()` alone (nothing further) — still `BRaw:5_1 @ 6144x3456p25`, no revert.
2. `select_clip()` + `enter_playback()` alone (no `exit_playback()` at all) — still
   `BRaw:5_1 @ 6144x3456p25`, no revert.
3. `select_clip()` + `enter_playback()` + `exit_playback()`
   (`play()`/`pause()`/`seek()`/`shuttle()`/`stop()` all skipped in between) — back to
   `ProRes:HQ @ 4096x2160p25`, the exact pre-`select_clip()` format.

Runs 1 and 2 rule out `select_clip()`/`set_camera_format()` and `enter_playback()` as the
cause; only removing `exit_playback()` from the sequence removes the revert.
**`exit_playback()`'s `PUT /transports/0 {"mode": "InputPreview"}` — leaving playback mode
— is what triggers the camera to restore whatever format preceded entry into `Output`
mode.** One caveat worth naming: in every test, `select_clip()` was the only thing that
ever changed format before `enter_playback()` ran, so "reverts to the pre-`select_clip()`
format" and "reverts to whatever format was active immediately before `Output` mode" are
indistinguishable from this evidence alone — they happen to be the same value in every
run. `exit_playback()` does not compensate for this or expose any way to opt out — a
caller that needs a specific format after leaving playback must call `set_camera_format()`
again explicitly afterward, not assume the camera holds `select_clip()`'s switch.

### `play()` / `pause()` / `stop()`

Thin aliases: `play()` is `shuttle(1.0)`, `pause()` is `shuttle(0.0)`, `stop()` is
`exit_playback()` — none of them touch the dedicated `/transports/0/play`/
`/transports/0/stop` trigger endpoints the migration plan originally named for them.

**Why routed through other endpoints instead of the plan's own names.** Both
`/transports/0/play` and `/transports/0/stop` are real (confirmed present by the Phase 0
read sweep) but sit in `probe_endpoints.py`'s `NEVER_WRITE` list — meaning, unlike
`/transports/0` and `/transports/0/playback`, **neither has ever had its `PUT` exercised at
all**, not even a same-value probe. Their request body (a void trigger needs *some* body —
`{}`? nothing?) and what a successful call would even look like on readback are both
unknown. `/transports/0/playback`'s `PUT` is confirmed writable (`204`, same-value probe,
and now a real changing-write sample too — see `shuttle()`/`seek()` below), so
`play()`/`pause()` route through it instead — trading a small amount of fidelity to the
plan's original API shape for a write path with real evidence behind it. `stop()` uses
`exit_playback()` for the identical reason, since `_set_transport_mode`'s body is
sweep-confirmed real. This is a considered scope reduction, not a silent one — if a real
hardware run shows `/transports/0/play`/`/transports/0/stop` behave differently from
"speed 1.0" / "leave playback mode," these aliases will need revisiting. Since `stop()` *is*
`exit_playback()`, the confirmed format-revert finding above (`enter_playback()` /
`exit_playback()` section) applies to `stop()` identically — calling it also restores
whatever format preceded playback mode.

**`/transports/0/play` and `/transports/0/stop`'s WS push shape confirmed real,
`POCKET_6K_G2 v8.6`, 2026-08-05 (Phase 8 item 2, `tools/rest/watch_events.py` run
alongside a full `rest_playback.py` cycle)** — still writes-untested (the paragraph above
is unchanged), but the *read* side now has direct evidence: both push plain booleans
(`true`/`false`, not an object), matching `Notification.yaml`'s schema exactly, and their
values track `/transports/0/playback`'s `speed` field precisely as that spec's own
descriptions say — `play` went `true` the instant `speed` left `0` (both forward `1.0`/`2.0`
and reverse `-1.0`), `stop` went `true` the instant `speed` returned to `0` (`pause()` and
`stop()` alike). No unsubscribed-and-therefore-silent gap here: `RestCameraSession` doesn't
subscribe these two by default, but a caller that subscribes them independently (as this
run did) gets real, correctly-computed events. Still no channel of any kind reports *why* a
transition happened — only that it did, the same ceiling every other camera-initiated-stop
detection in this codebase has. `/transports/0/playback`'s own `position` field advanced
smoothly through this run at a rate matching the clip's `25fps` at `speed=1.0` and roughly
double/reverse at `speed=2.0`/`-1.0` — real, frame-position-accurate, not merely present.

### `shuttle(speed)` / `seek(position)`

Both `PUT /transports/0/playback`, dual-check verified via the generic `_put_playback()`
helper: a WS `propertyValueChanged` event on `/transports/0/playback` primary, a `GET`
readback secondary, checked with `_contains()` — a structural "does the reported body
contain every key/value pair I just sent" match, not a named-field parser like
`_recording_flag`/`_format_matches`.

**Real body, `POCKET_6K_PRO v8.6`, 2026-08-04 (operator testing directly on real
hardware — this endpoint's first captured *changing* write, distinct from
`probe_endpoints.py`'s own same-value sweep):**
`{"type": "Play", "loop": bool, "singleClip": bool, "speed": float, "position": int}`.
`type: "Play"` covered both a paused view (`speed=0.0` — the playback view opens, nothing
moves) and normal forward playback (`speed=1.0`); no other `type` value has been observed,
which retires the migration plan's earlier `"Shuttle"`/`"Jog"` guess for this field as
unconfirmed. `position` is the playback position on the timeline, in video frames — this
also disproves an earlier hypothesis that `/playback` reused `GET /transports/0/timecode`'s
own `{"timecode": ..., "clip": ...}` field names; the real field is `"position"`, with no
separate `"clip"` field at all. `seek()` was renamed from `seek(timecode, clip=0)` to
`seek(position)` to match.

**`_put_playback()` is now a read-modify-write**, not a bare partial `PUT`: it `GET`s the
current body, overlays only the fields the caller is changing, and `PUT`s the merged
result — the same discipline `set_camera_format` uses for `/system/format` (design
principle 1), so `shuttle()` doesn't reset `loop`/`singleClip`/`position` to some invented
default, and `seek()` doesn't reset `type`/`loop`/`singleClip`/`speed`. `type`, `loop`, and
`singleClip` are not yet exposed as their own parameters; every write through `shuttle()`/
`seek()` leaves them at whatever the preceding `GET` reported.

`shuttle(speed)` sends `{"speed": speed}` as its overlay — positive shuttles forward,
negative backward, magnitude sets the rate. `0.0`/`1.0` (`POCKET_6K_PRO v8.6`, operator
Postman testing) and `2.0`/`-1.0` (both cameras, `examples/rest_playback.py`'s
forward/backward steps) are all real-hardware-confirmed now; other magnitudes remain an
unconfirmed extrapolation from the same field. `seek(position)` sends
`{"position": position}` — `seek(0)` itself is real-hardware-confirmed on both cameras.

**Why `_contains()` instead of a named parser.** Even with a real sample now in hand, the
confirmed body was never independently captured as a `GET /transports/0/playback` response
shape on its own (only through the request/observed-behavior pairing above) — `_contains()`
checks the reported body contains every key/value pair the call actually changed, not the
full merged body, so a field this call didn't touch can't cause a false failure or a false
"confirmed" either way.

### `playback_interrupted` / `wait_for_playback_interrupt(timeout)` (Phase 8 item 2, part 2)

The playback analogue of `is_recording`/`wait_while_recording`'s camera-initiated-stop
detection — built on the same two now-subscribed properties Part 1 confirmed real
(`PLAY_PROPERTY`/`STOP_PROPERTY`, see the `play()`/`pause()`/`stop()` section above),
plus `PLAYBACK_PROPERTY` and `TRANSPORT_MODE_PROPERTY`, both already subscribed since
Phase 7.

**Why `PLAYBACK_PROPERTY`'s own `speed` field is the trigger, not `STOP_PROPERTY`, even
though Part 1 confirmed `stop` tracks `speed` reaching `0` precisely.** `_put_playback()`
arms and waits on `PLAYBACK_PROPERTY` for its own dual-check, so the exact WS delivery that
satisfies a self-requested `pause()`/`shuttle()` is the *same* delivery `_on_event`
receives — and `RestEventRouter.handle_event()` always calls the `on_event` hook before it
wakes any `wait_for()` waiter (`events.py`), so `_playback_write_in_flight` is guaranteed
still `True` at the exact moment `_on_event` sees that event. `STOP_PROPERTY` is a second,
independently-pushed property with no such ordering guarantee relative to `_put_playback()`'s
own completion — using it as the trigger would leave a real race window between
`_playback_write_in_flight` being cleared (in a `finally`, right after the write's own
dual-check finishes) and `stop`'s own push arriving. `STOP_PROPERTY`/`PLAY_PROPERTY` are
still subscribed and tracked (`last_known_play`/`last_known_stop`), just as a corroborating
signal rather than the authoritative trigger. `TRANSPORT_MODE_PROPERTY` reporting a mode
other than `"Output"` gets the same "arrived while nothing of mine was in flight" test,
guarded by `_transport_mode_write_in_flight` — the analogous guard for
`enter_playback()`/`exit_playback()`'s own write path.

**Trigger condition: deviation from `_expected_speed`, not a fixed `speed == 0` check.**
Changed on request after the first two real-hardware runs (below) confirmed a fixed
`speed == 0` trigger worked — that check has a real ceiling: it can only ever catch a full
stop, not a camera-initiated speed change that lands anywhere else (e.g. `2.0` dropping to
`1.0` on its own) — which is exactly as much "not what this session asked for." Now compares
the pushed `speed` against `_expected_speed` instead — the speed value this session's own
last confirmed `enter_playback()`/`shuttle()`/`play()`/`pause()` call actually set.
`enter_playback()` resets it to `0.0` (the camera opens playback paused — see
`_put_playback`'s real-body finding above), and `_put_playback()` updates it after every
write it confirms carries a `speed` change. If `_expected_speed` is still `None` (no
baseline yet), nothing can be judged an interrupt — the same "not enough information to say"
posture `wait_for_low_storage()` takes on an unset `last_known_storage`. **Real-hardware-
confirmed as a working trigger, runs 4-5 below** — but both those runs' interrupts still
happened to land on `0` (`last_known_stop` corroborating `True` in both), so the specific
"deviates to a nonzero value" branch this broadening exists for remains real-hardware-
unconfirmed; a full stop is the only camera-initiated transition observed so far, on any
run against either version of the check.

`_in_playback` (whether `enter_playback()` has confirmed and `exit_playback()`/`stop()`
hasn't yet) and `playback_interrupted` are both set explicitly by `enter_playback()`/
`exit_playback()` themselves, not left entirely to `_on_event` — a write confirmed only via
its secondary `GET` readback (no WS event arrives at all) would otherwise leave
`_in_playback` never set. `enter_playback()` also clears `playback_interrupted` on success,
mirroring `wait_while_recording`'s "clear the flag before waiting" discipline, so a stale
interrupt left `.set()` by an *earlier* playback cycle can never be mistaken for a fresh one.

`wait_for_playback_interrupt(timeout)` mirrors `wait_for_low_storage`'s shape and polarity
exactly: `True` if an interrupt was observed (already set when called, or a qualifying event
arrived before `timeout`), `False` if `timeout` elapses with nothing observed. Only
meaningful while `_in_playback` is `True` — calling it outside that window just times out.

**Honest ceiling, same as every other camera-initiated-stop detection in this codebase**:
this reports that playback stopped unexpectedly, never why — there is no error/fault event
channel on this camera's REST API at all (see item 3's write-up above). A caller wanting the
reason still has to look at the camera itself.

**Status: built, unit-tested (`TestPlaybackInterrupted`, `TestWaitForPlaybackInterrupt`), and
real-hardware-confirmed end to end on both the original fixed-`speed == 0` trigger and the
current `speed != _expected_speed` broadening** — five runs total, `POCKET_6K_G2 v8.6`,
2026-08-05. `tools/rest/verify_playback_interrupt.py` is the verification script, following
item 1's `verify_low_storage.py` precedent: a self-requested sanity phase (`select_clip` ->
`enter_playback` -> `play` -> `pause` -> `play` -> `stop`, asserting `playback_interrupted`
stays clear throughout) followed by the real positive case (`play()`, then pull the card or
press stop/pause on the camera body, then `wait_for_playback_interrupt()`).

**Run 1 — real-hardware-confirmed defect, found and fixed same day, `POCKET_6K_G2 v8.6`,
2026-08-05, this tool's own sanity phase.** `stop()` reported `OK` (its own dual-check
passed), but `playback_interrupted` was set anyway — exactly the failure mode the sanity
phase exists to catch. Root cause: `stop()` -> `exit_playback()` ->
`_set_transport_mode("InputPreview")` only ever set `_transport_mode_write_in_flight`, not
`_playback_write_in_flight`. Leaving `"Output"` mode real-hardware-confirmed *also* pushes a
`PLAYBACK_PROPERTY` event reporting `speed: 0` as a side effect — the camera stopping
transport motion on its way out of playback, independent of anything `_put_playback()` did.
That side-effect push arrived while `_playback_write_in_flight` was correctly `False` (no
`_put_playback()` call was active) and `_in_playback` was still `True`, so the original
per-property guard — each branch checking only its own write path's flag — incorrectly set
`playback_interrupted` on a purely self-requested `stop()`. **Fixed**: both `_on_event`
branches now check *both* in-flight flags, not just the one matching their own property —
the two write paths turned out not to be as independent as the original design assumed.
Regression-tested (`test_speed_deviation_ignored_while_transport_mode_write_in_flight`,
`test_stop_with_side_effect_playback_event_does_not_set_interrupted` — the latter reproduces
the exact real-hardware sequence, delivering both the confirming `TRANSPORT_MODE_PROPERTY`
event and the side-effect `PLAYBACK_PROPERTY` event together). The symmetric direction (a
`_put_playback()` write causing a side-effect `TRANSPORT_MODE_PROPERTY` push) has no
real-hardware evidence either way yet, but both branches now guard against it defensively —
see `_on_event`'s own docstring for the full finding.

This same run's `select_clip()` step failed on the already-documented sensor-resolution
ambiguity (the default clip this tool picked, `clip_unique_id=1`, was the exact
`ProRes:HQ @ 1920x1080p25` three-way-ambiguous case from `select_clip()`'s own docstring) —
expected behavior, not a defect in this feature; the tool doesn't yet compose the
sensor-resolution retry `examples/rest_playback.py` uses.

**Run 2 — full end-to-end confirmation, same day, `--clip-id 2`** (a `.braw` clip,
sidestepping run 1's unrelated `select_clip()` ambiguity). **Phase 1 (sanity) passed
clean**: `select_clip` -> `enter_playback` -> `play` -> `pause` -> `play` -> `stop`, with
`playback_interrupted` staying clear after every step — confirming the run 1 fix.
**Phase 2 (the real positive case) also confirmed**: after `play()`, an out-of-band
interrupt (card pulled or stop/pause pressed on the camera body) was detected by
`wait_for_playback_interrupt()` in `13.5s`, returning `True`. `last_known_stop` corroborated
(`True`), and `GET /transports/0` still reported `mode: "Output"` — i.e. this was a
speed-only transition, caught by the (at-the-time) fixed `speed == 0` check, not a mode
exit. This is the first real-hardware evidence that the whole feature — subscription,
in-flight guards, and the actual camera-initiated interrupt — works end to end.

**Run 3 — third confirmation of the same fixed-`speed == 0` check, `--clip-id 2`, still
same day, run *before* pulling the commit that broadened the trigger.** Both phases clean;
`wait_for_playback_interrupt()` returned `True` after `19.1s`. The printed
`last_known_play`/`last_known_stop` snapshot at read time was `True`/`False` — at first
glance the *opposite* of run 2's stopped-looking signature — but this is a timing artifact,
not a different interrupt shape: `last_known_play`/`last_known_stop` are live, continuously-
updating fields (`_on_event`), and the fixed-`0` check that actually fired requires `speed`
to have hit `0` at some point regardless of what these two booleans show *afterward*. The
most likely read: the operator resumed playback (or the camera did) between the interrupt
firing and this print statement executing, moving `speed` back off `0` again before the
values were captured. Not itself concerning — `playback_interrupted`'s own `set()` already
happened and is what `wait_for_playback_interrupt()` reports; nothing about this run
suggests the trigger fired on a false signal.

**Runs 4 and 5 — first real-hardware runs of the broadened `speed != _expected_speed`
trigger**, both `--clip-id 2`, same day, after pulling the broadening commit. Run 4: both
phases clean, interrupt detected in `6.2s`, `last_known_play=False`/`last_known_stop=True`
at read time — a clean stopped signature. Run 5 (same, with `DEBUG` logging enabled,
showing every `GET`/`PUT`/`POST` call and its status code): both phases clean, interrupt
detected in `3.2s`, same `last_known_play=False`/`last_known_stop=True` signature. **Both
confirm the broadened trigger works as a drop-in replacement for the fixed-`0` check** —
but neither run's interrupt happened to land anywhere other than `0`, so the specific
"deviates to a nonzero value" branch the broadening was written for (see the trigger-
condition paragraph above) still has no real-hardware run of its own. Every camera-
initiated interrupt observed across all five runs so far, on either version of the check,
has been a full stop — consistent with pulling a card or pressing stop/pause on the camera
body always halting transport motion outright, never leaving it at some other nonzero
speed. Exercising the nonzero-deviation branch for real would need a different kind of
interrupt than "the operator stops the camera" — not yet identified.

---

## Write verbs (Phase 10 — media device formatting)

### `device_info(device_name)` → `DeviceInfo`

`GET /media/devices/{deviceName}` — the `state` a media device reports (`"None"`,
`"Scanning"`, `"Mounted"`, `"Uninitialised"`, `"Formatting"`, `"RaidComponent"`, per
`MediaControl.yaml`'s `MediaDeviceInformation` schema). `device_name` is a
`StorageDevice.device_name` value (e.g. `"sd0"`, sourced from `storage_state()`), not a
`/mounts/...` mount name (`mount_names()`'s `"A001-sd1"` style — a different string,
confirmed elsewhere in this codebase, `rest/media.py`'s module docstring) — this endpoint
takes the device name specifically, per the spec's own parameter description ("as returned
by deviceName member of Workingset or ActiveMedia"). `state` is kept as a plain `str`, not a
Python enum — the spec is the source of truth for the exact allowed values, and hardcoding a
validated set here would just be another way to be wrong about a value this codebase hasn't
independently confirmed on real hardware yet (design principle 6). Used internally by
`format_device()`'s completion poll; also a plain read verb on its own. **Real-hardware-
confirmed, `POCKET_6K_G2 v8.6`, 2026-08-13**: `state` observed moving `"Mounted"` ->
`"Formatting"` -> `"Mounted"` across `format_device()`'s successful run — see that method's
section below.

### `doformat_supported_filesystems()` → `tuple[str, ...]`

`GET /media/devices/doformatSupportedFilesystems` — the filesystem names `format_device()`
will accept (spec example: `["ExFat", "HFS"]`). `format_device()` validates its own
`filesystem` argument against a live call to this, the same live-capability-over-hardcoded-
assumption discipline `set_camera_format()` already uses for codec/resolution/fps (design
principle 7's REST sibling). **Real-hardware-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-13**:
returned `("ExFAT", "HFS")` — note the real camera's casing (`"ExFAT"`) differs from the
spec's own example (`"ExFat"`); a script hardcoding the spec's casing instead of this live
value gets rejected by `format_device()`'s own validation (see below) rather than silently
sending the wrong string.

### `format_device(device_name, *, confirm, filesystem, volume=None, timeout=120.0, poll_interval_s=1.0)`

Formats a media device — `GET .../doformat` for a one-time `key`, then `PUT .../doformat`
with `{key, filesystem, volume}` — per the official BMD REST spec (`MediaControl.yaml`), the
**only** media-erasure capability the REST API exposes at all. This is a direct consequence
of reading all 11 official OpenAPI/AsyncAPI spec files the user supplied for this phase: none
of them documents a per-clip or per-still delete endpoint anywhere. `TimelineControl.yaml`'s
`DELETE /timelines/0` only clears the timeline *object* (already implemented via
`select_clip()`), never touching clip files on disk. Whether the separate `/mounts/...`
filesystem surface (`rest/media.py`'s `list_mount`/`path_exists`, used for photo-capture
confirmation) supports `DELETE` for individual files is a genuinely open question — explicitly
deferred, at the user's own request, to a later investigation; this method does not touch that
surface at all.

**This erases every clip and every still on `device_name`, irreversibly.** `confirm` has no
default — a caller must pass `confirm=True` explicitly, or an omitted/`False` value raises
`ValueError` before a single request is sent. This is deliberately stricter than every other
write in this codebase (none of which gate on an explicit confirm flag), because none of them
are destructive in this specific, unrecoverable way — `record_start`/`set_camera_format`/
`select_clip` all change state the camera can be asked to change back; a completed format
cannot. `examples/rest_format_device.py` layers two more gates on top of this one (a printed
`storage_state()` before anything happens, and a prompt requiring the operator to type the
exact device name back) — see that script's own docstring.

**Capability check deliberately does not mirror `set_camera_format`/`_put_playback`'s own
pattern of gating on `endpoint.put_supported`.** `tools/rest/probe_endpoints.py`'s
`NEVER_WRITE` list includes this exact path (`/media/devices/{deviceName}/doformat`) — the
sweep tool refuses to PUT it even as a same-value probe, since there is no such thing as a
harmless format probe. `put_supported` is therefore structurally always `None` for this
endpoint; gating on it the way every other write here does would make `format_device()`
permanently unusable regardless of real camera support. This method instead gates on
`endpoint.supported` — the GET side, confirming the endpoint exists and actually returns a
format key — the only sweep-confirmed signal this endpoint can ever carry. Real PUT
capability rests on the official spec being accurate, not on a sweep probe, for this one
endpoint only.

**`filesystem` is a required argument, not the optional one `MediaControl.yaml` itself
describes it as.** Real-hardware-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-13: the first
version of this method took the spec at its word and left `filesystem` optional, omitting it
from the `PUT` body when not given — the camera rejected that with `400 {"error": "Field
'filesystem' missing from request body."}`. The spec's "optional" claim is simply wrong for
this firmware; real hardware overrides documentation here (design principle 6). `filesystem`
is validated against a live `doformat_supported_filesystems()` call before any write is
attempted, raising `BMDUnsupportedError` if the camera doesn't currently offer it — the same
live-capability-over-hardcoded-assumption discipline `set_camera_format` uses for
codec/resolution/fps (design principle 7's REST sibling). There is no way to read a device's
*current* filesystem to default to it instead — `Workingset`'s schema has no such field — so
requiring the caller to name one explicitly is the only safe option; `examples/rest_format_device.py`
prints `doformat_supported_filesystems()`'s live result before prompting for exactly this
reason.

**`volume` turned out to be effectively required too, but for a different reason, and the
first run's reading of it was wrong.** A second real-hardware run, same camera/firmware/day,
with `filesystem` now supplied, got `400 {"error": "Field 'volume' missing from request
body."}` once `filesystem` stopped being the blocking field — disproving the first run's
"`volume` was not rejected, so it remains optional" conclusion. What actually happened: the
camera validates the body one field at a time and simply never got past `filesystem` on the
first run to check `volume` at all; the "no error about volume" signal was silence from a
validator that hadn't looked yet, not confirmation the field is optional. Unlike `filesystem`,
this codebase *can* read a device's current volume — `StorageDevice.volume`, from
`storage_state()` — so `volume: str | None = None` keeps its signature and its "omit to keep
the current name" behavior, but that behavior is now real: when `volume` is not given, this
method fetches `storage_state()`, finds `device_name`'s entry, and sends its current `volume`
as the value — rather than omitting the field (now known to fail) or guessing a name. If
`device_name` isn't present in `storage_state()`, or is present with no `volume` of its own,
this raises `ValueError` before any request is sent — there is no safer fallback left at that
point.

**Verification is structurally weaker than every other write in this codebase, stated
plainly rather than overstated away.** `Notification.yaml`'s `deviceProperty` enum — the
complete list of WS-subscribable properties, confirmed by reading the spec directly — has no
entry for any `/media/devices/...` path. There is no WS event for format progress or
completion at all, so design principle 3's "event primary, GET readback secondary" dual-check
cannot apply here; the only verification signal is polling `device_info(device_name).state`.

A poll immediately after the `PUT` risks reading a stale `state` left over from before the
format actually began (the camera has not necessarily transitioned out of `"Mounted"` yet)
and mistaking that for a false-positive completion. To guard against exactly that, this
method requires **observing `state == "Formatting"` at least once** before it will accept a
later terminal state (`"Mounted"`, `"Uninitialised"`, or anything else that isn't
`"Formatting"`) as genuine completion — a terminal state other than `"Mounted"` is accepted
deliberately, since the spec does not promise a freshly formatted device always lands there,
and refusing to recognize a real terminal state would just be a different kind of wrong guess
about the camera's behavior. Raises `BMDVerificationError` if `timeout` elapses without ever
observing both a `"Formatting"` state and a subsequent terminal one.

**Three real-hardware runs, `POCKET_6K_G2 v8.6`, 2026-08-13** (via `examples/rest_format_device.py`).
The first two confirmed the capability check, the typed-confirmation flow, and the `GET` key
round trip all work end to end, and both reached a real `PUT` — the first failing on the
missing-`filesystem` defect above, the second (after that fix) failing on the missing-`volume`
defect above. Neither request reached the camera's format logic at all — both were `400`s up
front, not failures partway through, so nothing on the card was touched in either run.

**The third run, with both fields fixed, succeeded end to end** — the first real-hardware
confirmation of the polling/completion path, not just the setup steps before it. `PUT` sent at
`15:37:02.796`; `device_info("sd0").state` observed moving `"Mounted"` -> `"Formatting"` ->
`"Mounted"`; completion logged at `15:37:07.871` — a real full-card format (1TB,
`filesystem="ExFAT"`, `volume` defaulted to the card's existing `"A002"`) in **~5 seconds**,
comfortably inside the default `timeout=120.0`/`poll_interval_s=1.0`. The resulting
`storage_state()` confirms it actually happened: `clip_count` `20` -> `0`, `remaining_space`
restored to within ~33MB of `total_space` (filesystem overhead). This is the first
real-hardware confirmation that the `"Formatting"`-must-be-observed-first guard works on the
success path — the first poll after the `PUT` already found `"Formatting"`, so the guard
never had to reject a stale read on this run. Its *correctness* is confirmed; its *necessity*
(rejecting a genuinely stale `"Mounted"` read immediately after the `PUT`) still has no
real-hardware reproduction of its own — that would need a poll landing before the camera's own
transition out of `"Mounted"`, which this run's timing didn't produce.

---

## Write verbs (Phase 11 — clip deletion)

### `delete_clip(clip_unique_id, *, confirm)` → `Clip`

`DELETE`s one clip's real `/mounts/...` path — the capability
`tools/rest/probe_endpoints.py`'s `--probe-mounts-delete`/`--delete-real-file` investigation
(`docs/rest/transport.md`'s Mode 3 section) exists to answer, and now has a real answer:
**`DELETE` on `/mounts/...` really deletes a real clip file, real-hardware-confirmed,
`POCKET_6K_G2 v8.6`, 2026-08-13.**

That confirmation is of the underlying sequence, not of this method directly. The
investigation tool's own `--delete-real-file` attempt crashed before reaching its `DELETE` —
`request()` called `response.text()` on a real `.braw` clip's `application/octet-stream`
body, which isn't valid UTF-8 (fixed in `probe_endpoints.py` via a new `decode_body()`
helper; see that file's own section above). Since the tool crashed, the operator repeated
the exact same three-step sequence by hand in Postman instead — arguably stronger evidence,
since it rules out any bug in the tool's own status interpretation:

```
GET    /mounts/A002-sd1/A002_08120218_C001.braw  -> 200 (Content-Type: application/octet-stream)
DELETE /mounts/A002-sd1/A002_08120218_C001.braw  -> 200 OK
GET    /mounts/A002-sd1/A002_08120218_C001.braw  -> 404 Not Found
```

`delete_clip()` composes that confirmed sequence through this session's own machinery
(`clips()`, `rest/media.py`'s `resolve_active_mount()`, `RestClient.exists()`) — but has not
itself been run against real hardware yet. `examples/rest_delete_clip.py`'s first successful
run is what closes that specific gap.

**Only clip deletion is confirmed — there is no `delete_still()`.** No still's exact
`/mounts/...` path has been independently confirmed the way this clip's was, because the
Stills directory's own known `500` listing defect means one can't be read off a listing.
Plausibly the same mechanism; not proven. Building a still-deletion method is a separate
step for once that confirmation exists — not assumed from this one.

**`confirm` has no default**, mirroring `format_device()`'s exact gate for the same reason:
this permanently erases the clip, and unlike `record_start`/`set_camera_format`/`select_clip`
it changes state the camera cannot be asked to change back. Omitting it or passing `False`
raises `ValueError` before a single request is sent.

`clip_unique_id` is resolved against a fresh `clips()` call, raising `ValueError` if it isn't
found — the same discipline `select_clip()` already uses. Remember `clip_unique_id` is **not
stable across reconnects** (`clips()`'s own section above) — resolve it fresh in the current
session, never reuse an id captured earlier.

**The real `/mounts/...` path is built from a single confirmed real-hardware sample, not a
general rule.** The one clip this was confirmed against, `Clip.file_path`
`/mnt/sd0/A002/A002_08120218_C001.braw` (the internal camera path `clips()` reports), sits at
`/mounts/A002-sd1/A002_08120218_C001.braw` over REST — the file directly under the mount
root, with the internal path's `/A002/` reel subdirectory **not** present in the HTTP layout.
`delete_clip()` reuses `resolve_active_mount()` for the mount root (the camera's own real
mount listing, never a `deviceName`-to-mount-suffix guess) and appends only `file_path`'s
basename, mirroring the one confirmed sample exactly. A camera or firmware where clips sit in
a real subdirectory under the mount root would break this — no second data point exists yet
to know whether that's ever the case, the same honesty `resolve_mount_path()`'s own docstring
already carries for the mount-*selection* side of this same problem.

**Verification is `RestClient.exists()` before and after the `DELETE` — never a plain
`get()`.** A clip's body is binary (`application/octet-stream`); `exists()` is specifically
built to never attempt to parse or decode it (its own docstring names exactly this class of
crash — the one `probe_endpoints.py`'s `request()` hit before being fixed). Raises
`BMDVerificationError` before sending `DELETE` at all if the resolved path doesn't exist yet
(the path assumption above was wrong, or the clip is already gone), and again if the path
still exists immediately after `DELETE`. No polling loop: the confirmed real sequence
completed synchronously — unlike `format_device()`'s multi-second full-card format, nothing
in the Postman trail suggested an intermediate "still processing" state to wait through.

`RestClient.delete()` gained an `api_prefixed: bool = True` parameter for this
(`/mounts/...` is outside `API_BASE`, the same reason `get()`/`exists()` already have it) —
it previously had no way to reach `/mounts/...` at all.

**Real-hardware-confirmed end to end, `POCKET_6K_G2 v8.6`, 2026-08-13, twice**
(`examples/rest_delete_clip.py`): recorded a real 10s clip, identified it via
`confirm_new_clip()`, and deleted it — the `exists()`/`DELETE`/`exists()` sequence confirmed
exactly as designed both times, matching the Postman trail precisely.

| Run | `clip_unique_id` | file | `DELETE` sent | confirmed gone | elapsed |
|---|---|---|---|---|---|
| 1 | `23` | `A002_08120328_C003.braw` | `16:49:30.754` | `16:49:31.917` | ~1.2s |
| 2 | `24` | `A002_08120336_C004.braw` | `16:57:58.095` | `16:57:59.246` | ~1.15s |

**But `GET /clips/list` still reported the clip immediately afterward, in the same session,
on both runs** — the printed "AFTER deletion" clip inventory showed `2 clip(s) on card` each
time, the same count as immediately after recording, not `1`. The file itself was genuinely
gone (`delete_clip()`'s own `exists()` check, the same real mechanism Postman verified,
confirmed this correctly) — `/clips/list` simply didn't reflect that change in the same
breath. This does not put `delete_clip()`'s own verification in question: it attests to the
file's real existence at its real path, never to `/clips/list`'s contents, and that
attestation was and remains correct.

**Resolved, not left open: a fresh reconnect clears it.** Immediately after run 2,
`examples/rest_read_state.py` connected fresh at `16:58:47.388` — roughly 48 seconds after
run 2's own confirmation — and reported `Active storage: sd0 (74437s remaining, 1 clips)` and
`Clips: 1`, both correctly reflecting reality (only the original pre-existing clip remained;
both test clips from runs 1 and 2 were genuinely gone). The staleness is a same-session
artifact, not a permanent index corruption or a sign either deletion was somehow incomplete.
Whether it would also clear within the *same* session without reconnecting — immediately, or
after some shorter delay — remains untested; only "still stale immediately" and "correct
after a reconnect ~48s later" are actually confirmed. `delete_clip()` still makes its
best-effort `clips()` check (`BMDStorageError`-swallowing, never raising) and logs a
`WARNING` if the id is still listed, purely informational (design principle 9) — a caller
that needs `/clips/list` to agree immediately should reconnect rather than poll within the
same session.

**The "no polling loop" call above was retracted one day later — `POCKET_6K_G2 v8.6`,
2026-08-14, via `examples/rest_delete_clips_bulk.py`, both of its real runs.** Bulk-deleting
three freshly-recorded clips in a loop hit `BMDVerificationError: still exists after DELETE`
repeatedly — run 1: 1 of 3 clips failed (`clip_unique_id=4`, `3` and `5` succeeded); run 2: 2 of
3 failed (`7` and `8`; only `6`, the first in the batch, succeeded). In both runs, **only the
first clip deleted in the loop ever succeeded on the first immediate check** — every clip after
it, deleted back-to-back with no natural gap between calls, hit a false "still exists" that had
never appeared across either of this method's earlier *isolated* single-clip runs. This is the
identical one-off camera-side propagation delay `delete_still()` had already been fixed for
(above) — a tight loop with no inter-call gap exercises that same race far more reliably than a
single standalone call ever did, which is exactly why two isolated real-hardware runs never
caught it. **Fixed the same way**: the after-`DELETE` check now polls (`poll_interval_s`,
default `0.5`s, bounded by `verify_timeout_s`) instead of checking once — the asymmetry this doc
drew against `delete_still()`'s fix (`delete_clip()` "deliberately not changed... both
real-hardware runs confirmed correctly on the first immediate check") no longer holds; that was
correct given the evidence available at the time, and the new evidence changed it.

**Third run, `POCKET_6K_G2 v8.6`, 2026-08-14 — the polling fix confirmed on real hardware.**
`examples/rest_delete_clips_bulk.py`, rerun against the fix: all 3 clips (`clip_unique_id`
`9`/`10`/`11`) deleted successfully — `delete_clips(): 3/3 deleted, 0 failed`, closing the gap
the first two runs opened. The polling fix works in exactly the back-to-back-loop scenario that
exposed the original race.

**Bonus finding from the same run, closing a previously-open question**: the `/clips/list`
same-session-staleness section above says clearing "within the same session without
reconnecting... remains untested." This run tested it, incidentally. `clips()` immediately after
deleting `clip_unique_id=9` still listed it (staleness, as expected); the equivalent check after
`clip_unique_id=10` did *not* list it (no staleness that time); the check after `clip_unique_id=11`
listed it again (staleness). The final "AFTER bulk deletion" inventory, printed under two seconds
after the first `DELETE`, reported `3` clips — not `5` (all three stale, the pattern every earlier
run showed) and not `2` (the true count). One stale entry had already cleared **within the same
session, without reconnecting, in well under two seconds** — faster than the ~48-second
reconnect-driven clearing observed earlier, and without a reconnect at all. This refines rather
than contradicts the earlier finding: same-session staleness is real, but not fixed at "clears
only via reconnect after ~48s" — it can also self-resolve within the same session, apparently on
its own schedule, sometimes for one clip in a batch and not (yet) for another. Still purely
informational; `delete_clip()`'s own file-level verification was and remains the authoritative
signal throughout.

### `delete_still(path, *, confirm)` → `None`

`DELETE`s one still's real `/mounts/.../Stills/...` path. **Real-hardware-confirmed
working, `POCKET_6K_G2 v8.6`, 2026-08-13** — done by hand in Postman, the same
investigation method `delete_clip()`'s own confirmation was built on:

```
GET    /mounts/A002-sd1/Stills/A002_08120219_S001.braw  -> 200
DELETE /mounts/A002-sd1/Stills/A002_08120219_S001.braw  -> 200 OK
GET    /mounts/A002-sd1/Stills/A002_08120219_S001.braw  -> 404 Not Found
```

This method itself composes that confirmed sequence through `RestClient.exists()`/
`RestClient.delete()`.

**Unlike `delete_clip()`, this method takes the full `/mounts/...` path directly and never
tries to resolve or guess one itself.** `delete_clip()` can resolve `clip_unique_id` against
`clips()` because clips have that identifier and a working listing; stills have neither — the
Stills directory itself `500`s unconditionally on listing (`rest/media.py`'s module
docstring), so there is no `clips()`-equivalent to resolve against and no still-id to accept
in its place. `rest/media.py`'s `guess_new_still_path()` is deliberately opt-in and
best-effort by design — never a source of truth for anything, let alone a destructive target
(design principle 7's "never guess" discipline, extended here to the caller's own
responsibility rather than something this method could safely do internally). Obtain `path`
from `guess_new_still_path()` (after independently confirming a photo was taken via
`wait_for_new_still()`) or from manual investigation — the same way the real-hardware
confirmation above was obtained.

**First `examples/rest_delete_still.py` run, `POCKET_6K_G2 v8.6`, 2026-08-13** — capture
confirmed (Stills `mtime` changed within the timeout), but `guess_new_still_path()` returned
`None`: every `(minute_offset, index, extension)` candidate around the BLE trigger's PC-clock
timestamp (`17:23:06`) missed, so the script correctly stopped without deleting anything
(design principle 7). The operator located the real filename by another means —
`A002_08120402_S009.braw` — a camera-clock timestamp of `08-12 04:02`, ~37h21m behind the PC
clock at trigger time. **Root cause confirmed three independent ways**: this offset matches,
within about a minute, two earlier timestamp-offset samples computed from a clip recording and
a still capture the same day, and the camera's own SETUP screen (photographed the same
session) read `DATE AND TIME: 2026/08/12 - 04:09` — the camera's onboard clock had simply never
been set correctly, not a defect in `guess_new_still_path()`'s logic. This is a permanent,
now-documented limitation of the guessing approach — see `rest/media.py`'s module docstring
("CAMERA CLOCK SKEW BREAKS THE 'SAME MINUTE' ASSUMPTION") for the full write-up and the guidance
for a caller working against a camera with a known-wrong clock. `delete_still()` itself was not
exercised by this run — the script correctly stopped one step before it, at the guess.

**Second run, same camera/firmware/day, closes the gap — with a real defect found and fixed.**
A small standalone script (bypassing `guess_new_still_path()` entirely, calling `delete_still()`
directly against the real path the operator had obtained by other means,
`/mounts/A002-sd1/Stills/A002_08120402_S009.braw`) reported `BMDVerificationError: still exists
after DELETE — not confirmed deleted`. But the operator independently confirmed, by checking the
SD card's own contents, that the still really was gone. **This was a false negative in the
verification's timing, not a failed deletion.** The single, immediate `exists()` call right
after `DELETE` raced a brief camera-side propagation delay — a one-off finalization window, the
same shape `record_stop` hit and was fixed for (`stop_verify_timeout_s`, see that method's
section above). The earlier Postman confirmation of this exact sequence never hit this, because
a human clicking `DELETE` and then `GET` naturally leaves a several-second gap; two back-to-back
automated requests do not.

**Fixed**: the after-`DELETE` check now polls (`poll_interval_s`, default `0.5`s, bounded by
`verify_timeout_s`, mirroring `select_clip()`'s existing poll shape) instead of checking once.
`delete_clip()` was, at the time, deliberately left unchanged — both of its own real-hardware
runs had confirmed correctly on the first immediate check, so widening it looked speculative
rather than evidence-driven. **That reasoning was retracted one day later** — see
`delete_clip()`'s own section below for the bulk-deletion run that found the identical race in
that method too, and the same polling fix applied there.

**Third run, `POCKET_6K_G2 v8.6`, 2026-08-13 — the polling fix confirmed on real hardware.** The
same standalone script, rerun against the fix, deleted the still successfully with no
`BMDVerificationError` — closing the gap the second run's false negative opened.
`delete_still()`'s full real-hardware trail across all three runs: run 1 (via
`examples/rest_delete_still.py`) stopped correctly at the clock-skew guess failure before ever
reaching this method; run 2 (standalone script, real path, pre-fix) reached it and hit the
polling race; run 3 (same standalone script, post-fix) succeeded. `delete_still()` composed
through `RestCameraSession`'s own machinery end to end is now real-hardware-confirmed, the same
status `delete_clip()` already carried.

`confirm` has no default, mirroring `delete_clip()`/`format_device()`'s exact gate.
Verification is `RestClient.exists()` before and after `DELETE` — never a plain `get()`,
since a still's body is binary (a real `.dng`/`.braw` image), the same reasoning
`delete_clip()` uses for a clip's body. No polling loop — the confirmed real sequence
completed synchronously, the same shape `delete_clip()` showed for a clip file. There is no
`/clips/list`-equivalent listing for stills to check for the kind of same-session staleness
`delete_clip()` found and warns about — Stills can't be listed at all, confirmed or stale, so
no analogous best-effort check is possible here.

---

## Downloading clips/stills to the PC (Phase 12)

The mirror-image capability of Phase 11's deletion: copy a real clip or still off the camera's
`/mounts/...` filesystem to local disk, instead of erasing it. Built on the same
`resolve_active_mount()`/mount-path-construction machinery `delete_clip()`/`delete_still()`
already use, so it inherits their single-confirmed-sample caveat about the real path shape
unchanged (see `delete_clip()`'s docstring).

**Status: both `download_clip()` and `download_still()` real-hardware-confirmed,
`POCKET_6K_G2 v8.6`, 2026-08-14.** `examples/rest_download_still.py`'s first run
(`examples/capture_photo.py`'s BLE-trigger + REST-confirm composition, then
`guess_new_still_path()`, then the actual download) succeeded end to end: capture confirmed,
guess matched (`A002_08141103_S010.braw`, trigger at PC wall-clock `11:04:17` — the guess's
`minute_offsets` window found it on the first try, unlike the clock-skew failure `rest_delete_still.py`
hit on 2026-08-13; the camera's clock now agrees with the operator's PC, closing that finding's
practical impact for this session), and `download_still()` wrote `951400` bytes to
`examples/downloads/A002_08141103_S010.braw` in well under a second (~106 MB/s over USB) with no
`Content-Length` mismatch. `examples/rest_download_clip.py`'s first run also succeeded — reported
by the operator as working with no defects found; see `download_clip()`'s own entry below.

### `RestClient.download(path, dest, *, api_prefixed=False, chunk_size=1MiB, stall_timeout_s=30.0)` → `int`

The new transport primitive underneath both methods below (`rest/client.py`). A real clip runs
tens of GB (`rest_record_test_clip.py`'s real-hardware runs recorded a 600s clip whose
`bytes_written` alone was ~35GB) — `get()`'s `.json()`/`.text()` body handling would either raise
decoding binary content (the exact crash class `exists()` was built to avoid) or hold the whole
file in memory trying not to. `download()` instead streams via `aiohttp`'s
`resp.content.iter_chunked()`, writing each chunk with `asyncio.to_thread()` so a disk write
never blocks the event loop (design principle 11).

**Timeout is stall-based, not total-based** — the one place in this client that departs from
`DEFAULT_TIMEOUT_S`'s flat 5s budget, because a 5s *total* cap is nonsensical for a multi-GB
transfer whose total time is expected to be long. `stall_timeout_s` (default 30s) instead bounds
*inactivity*: no data received for that long means genuinely stalled, implemented via
`aiohttp.ClientTimeout(sock_connect=self.timeout_s, sock_read=stall_timeout_s)` with no `total`
cap at all.

Defaults to `api_prefixed=False` — the opposite of `get()`/`exists()`/`delete()` — since every
real caller downloads a media file from the `/mounts/...` namespace, never a control-API JSON
response.

Returns the number of bytes written. Raises `BMDRestError` if the server's own `Content-Length`
doesn't match what was actually received — a transport-level integrity check (design principle
5's boundary: this is raw HTTP correctness, not camera semantics, so it lives in `client.py`
rather than being layered on in `session.py` the way `delete_clip()`/`delete_still()`'s
camera-semantic `exists()`-before/after checks are). `501`/non-2xx/connection-failure handling
otherwise matches every other method's status contract.

### `download_clip(clip_unique_id, dest_dir, *, overwrite=False)` → `Path`

The mirror-image operation of `delete_clip()`, reusing its exact clip-resolution
(`clips()`) and mount-path-construction logic — the same single-confirmed-sample "basename
directly under the mount root, reel subdirectory dropped" rule applies unchanged here. Saves the
file as `dest_dir/<remote filename>`.

`dest_dir` must already exist (`ValueError` if not — this method never creates directories,
matching `format_device()`/`delete_clip()`'s "do exactly what was asked, nothing implicit"
discipline). If a file of that name already exists at the destination, raises `FileExistsError`
unless `overwrite=True` — checked before any network request, so a naming collision never costs
a wasted multi-GB transfer.

This is a read from the camera plus a local write, not a camera-state write, so design principle
3's dual-check discipline (echo/event primary, readback secondary) doesn't apply the same way it
does to `record_start`/`set_camera_format`. `RestClient.download()`'s own `Content-Length`
integrity check is this operation's verification.

**Real-hardware-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-14**, via `examples/rest_download_clip.py`:
worked with no defects found.

### `download_still(path, dest_dir, *, overwrite=False)` → `Path`

The mirror-image operation of `delete_still()`: takes the full `/mounts/.../Stills/...` path
directly rather than resolving one, for the identical reason `delete_still()` does — the Stills
directory `500`s unconditionally on listing, so there is no `clips()`-equivalent to resolve
against. Obtain `path` the same way `delete_still()`'s callers do — `guess_new_still_path()` or
manual investigation. Same `dest_dir`/`overwrite` contract as `download_clip()`.

**Real-hardware-confirmed, `POCKET_6K_G2 v8.6`, 2026-08-14**, via `examples/rest_download_still.py`:
a real photo triggered over BLE, confirmed over REST, its path resolved by
`guess_new_still_path()`, then downloaded — `951400` bytes written to a fresh local file, no
`Content-Length` mismatch, `overwrite=True` path exercised (the example always passes it, so the
`FileExistsError` branch specifically remains real-hardware-unexercised, though it's
straightforward and unit-tested).

---

## Bulk clip deletion (Phase 13)

### `delete_clips(clip_unique_ids, *, confirm)` → `BulkDeleteResult`

Deletes multiple clips in one call. Adds no new HTTP surface — it's built entirely on top of
`delete_clip()`, called once per id, so it inherits every one of that method's real-hardware-
confirmed properties (the `GET`/`DELETE`/`GET` sequence, the single-confirmed-sample real-path
construction, the known same-session `/clips/list` staleness and its best-effort `WARNING`)
unchanged.

**Status: real-hardware-confirmed end to end, `POCKET_6K_G2 v8.6`, 2026-08-14, three runs of
`examples/rest_delete_clips_bulk.py`** — records several disposable throwaway clips first (the
same self-contained safety pattern `rest_delete_clip.py` uses for one clip), so nothing it
bulk-deletes is ever irreplaceable footage. `delete_clips()`'s own batching/validation/partial-
failure logic worked exactly as designed across all three runs (bad-id-free batches, correct
deduplication, every clip attempted regardless of an earlier one's outcome, an accurate
`BulkDeleteResult` every time) — but the first two runs also surfaced a real defect **in
`delete_clip()` itself**, not in `delete_clips()`: see `delete_clip()`'s own section above for
the full write-up. In short, calling `delete_clip()` back-to-back in a loop (something no
earlier real-hardware run of `delete_clip()` alone had ever done) exposed a false-negative
verification race that a single isolated call had never hit, fixed by polling the after-`DELETE`
check the same way `delete_still()` already was. **The third run confirmed the fix**: all 3
clips deleted, `0` failed — see `delete_clip()`'s own section above for the full write-up,
including a bonus finding about `/clips/list` staleness clearing faster than previously observed
within that same run.

`clip_unique_ids` is de-duplicated (order-preserving) before anything else — passing the same id
twice can never attempt a second `DELETE` against an already-gone file.

**Validated against a single fresh `clips()` call before any `DELETE` is sent.** Every id must be
found, or `delete_clips()` raises `ValueError` naming every missing one and deletes nothing at
all — deliberately stricter than looping `delete_clip()` yourself, where a bad id near the end of
a large batch would only be discovered after everything before it was already, irreversibly,
gone. This is `delete_clip()`'s own "resolve first, then act" discipline, applied to the whole
batch atomically instead of one id.

**After validation, one clip's failure does not stop the batch.** This is the one place this
method's behavior diverges from every single-target write in this codebase: `delete_clip()`
itself is binary (confirmed or raises), but a bulk operation's partial success is a real, expected
outcome, not exceptional. Each id is deleted via `delete_clip(clip_unique_id, confirm=True)` in
turn; a `BMDVerificationError`/`ValueError` from any one is caught, logged at `ERROR`, and
recorded in `BulkDeleteResult.failed` (a `(clip_unique_id, exception)` pair) rather than raised.
`delete_clips()` itself never raises once past the up-front validation — a caller that wants
"raise if anything failed" checks `.failed` and raises itself.

`confirm` has no default, mirroring `delete_clip()`'s exact gate, checked before the validating
`clips()` call so an omitted/`False` value costs nothing.

---

## Bulk clip download (Phase 15)

### `download_clips(clip_unique_ids, dest_dir, *, overwrite=False)` → `BulkDownloadResult`

Downloads multiple clips in one call — the mirror-image of `delete_clips()` (Phase 13), same
reasoning, built the same way: adds no new HTTP surface, entirely composed from `download_clip()`
called once per id, so it inherits that method's real-hardware-confirmed clip-resolution/mount-
path/`Content-Length`-integrity behavior unchanged. Unlike `delete_clips()`, downloading is
non-destructive — nothing on the card changes, so there is no `confirm` gate here at all, matching
`download_clip()`/`download_still()`'s own signatures.

`clip_unique_ids` is de-duplicated (order-preserving) first, same as `delete_clips()`.

**Validated up front, before any network request, in two parts:**

1. `dest_dir` must already exist — `ValueError` if not, checked *before* the validating `clips()`
   call, so a bad destination costs nothing, not even the read that resolves the ids.
2. Every id must be found in a single fresh `clips()` call, or `download_clips()` raises
   `ValueError` naming every missing one and downloads nothing at all — `delete_clip()`'s own
   "resolve first, then act" discipline, applied to the whole batch atomically instead of one id,
   extended here with the `dest_dir` check `delete_clips()` has no analogous need for.

**After validation, one clip's failure does not stop the batch** — the same partial-success
philosophy `delete_clips()` established: a bulk operation's partial success is a real, expected
outcome, not exceptional, unlike a single `download_clip()` call's binary confirmed-or-raises
contract. Each id is downloaded via `download_clip(clip_unique_id, dest_dir, overwrite=overwrite)`
in turn; a `BMDRestError`/`BMDUnsupportedError`/`BMDConnectionError`/`FileExistsError`/`ValueError`
from any one is caught, logged at `ERROR`, and recorded in `BulkDownloadResult.failed` (a
`(clip_unique_id, exception)` pair) rather than raised. `download_clips()` itself never raises once
past the up-front validation — a caller that wants "raise if anything failed" checks `.failed` and
raises itself. The caught-exception set differs from `delete_clips()`'s
`(BMDVerificationError, ValueError)` because it matches `download_clip()`'s own actual failure
modes (transport/HTTP errors and local naming collisions), not camera-state verification errors.

`BulkDownloadResult.downloaded` holds `(clip_unique_id, Path)` pairs rather than `Clip` objects the
way `BulkDeleteResult.deleted` does — for a download, the new/interesting information is *where the
file landed locally*, not clip metadata (which `download_clip()` doesn't return in the first
place).

**Status: not yet real-hardware-run.** The real-hardware verification script is
`examples/rest_download_clips_bulk.py` — an ordinary capability-demonstration script, not an
edge-case-forcing one, so it lives under `examples/` rather than `tools/rest/`. It downloads the
first `MAX_CLIPS` (default 3) clips `clips()` reports (or an explicit `CLIP_UNIQUE_IDS` list) and
reports both per-clip and aggregate throughput (total bytes / total elapsed time) — since the
user's stated interest going forward is capabilities affecting SD card read/write speed, this
script doubles as a rough real-world read-speed measurement, not just a correctness check.

---

## Codec name mapping (`rest/mapping.py`)

`RestCameraSession`'s write path (`set_camera_format`) takes profile *names* — the
same vocabulary a BLE script already uses (`"BRAW"`/`"5:1"`, `"ProRes"`/`"422"`) — so a
script's vocabulary stays identical on either transport. `mapping.py` supplies the
translation:

- `derive_rest_codec_name(family, variant)` — the confirmed BRAW colon-to-underscore rule,
  e.g. `("BRAW", "5:1") -> "BRaw:5_1"`. A **derivation**, not a source of truth.
- `resolve_rest_codec_name(format_names, family, variant)` — prefers a profile's confirmed
  `rest/<fw>.json` `format_names` entry, falling back to `derive_rest_codec_name` only when
  unconfirmed.

The fallback exists because the derivation is wrong for at least one real case: REST
spells ProRes's `"422"` variant `"Original"`, which no rule can derive
(docs/rest/transport.md, "Codec naming"). This is exactly design principle 1 in miniature:
confirmed evidence always wins over a plausible-looking rule.

Resolution and fps need no lookup at all — `"4K DCI" -> {width: 4096, ...}` already comes
from the BLE profile's existing `resolutions` table (shared across transports), and
REST's `frameRate` strings (`"23.98"`, `"24"`, ...) already match this repo's `fps_modes`
keys directly (`Notification.yaml`'s `FrameRate` enum).

---

## Timecode decode (`rest/timecode.py`)

`GET /transports/0/timecode` returns `{"timecode": <int>, "clip": <int>}`, where
`timecode` is BCD-packed `HH:MM:SS:FF`, **big-endian** — the opposite byte order from the
BLE `TIMECODE` characteristic's `[frames, seconds, minutes, hours]` (docs/ble/timecode.md).
`decode_rest_timecode` shifts the four BCD bytes out of the 32-bit integer
(hours = bits 24-31, frames = bits 0-7) and reuses the BLE `Timecode` dataclass —
transport differs only in the wire decode, not in the resulting value or
`duration_seconds()`'s math.

Confirmed on real `POCKET_6K_G2 v8.6` hardware (2026-08-03): `{"timecode": 274153986}` ==
`0x10574202` == `10:57:42:02`, matching the time-of-day the sweep actually ran at.

---

## Testing

`tests/unit/rest/test_rest_session.py` bypasses `RestCameraSession.__init__`'s real
`CameraProfile.for_model` lookup via `__new__` (the same pattern
`tests/unit/test_session.py` uses for the BLE `CameraSession`), injecting a minimal fake
`RestClient`-like object for read-verb tests and a combined fake `aiohttp.ClientSession`
(supporting both `RestClient`'s `.request()` and `RestEventRouter`'s `.ws_connect()`) for
connection-lifecycle tests. `test_mapping.py` and `test_rest_timecode.py` cover the pure
derivation/decode functions directly, including the real captured timecode value above.

`record_start`/`record_stop` reuse the same fake `RestClient`, extended with a `.put()`
that records every call and returns a canned response (`None` by default, matching a real
`204`). The WS-event primary path is exercised by calling the session's real
`RestEventRouter.handle_event()` directly from a background `asyncio.Task` scheduled just
before awaiting `record_start()`/`record_stop()` — the same "deliver later, from a
concurrent task" shape `TestWaitWhileRecording`'s tests already use for the read side — so
`wait_for()`'s actual freshness/staleness logic runs, not a mock standing in for it. The
GET-readback secondary path is exercised by never delivering an event and shortening
`verify_timeout_s` to keep the test fast.

`set_camera_format` reuses the same fake-`RestClient`/deliver-an-event pattern, plus a
`make_format_profile()` helper carrying both a `codecs`/`resolutions`/`fps_modes` BLE table
(mirroring `tests/unit/test_session.py`'s `make_settings_profile`) and a `rest/` profile
confirming `/system/format`'s `put_supported` and `/system/supportedFormats`'s `supported`.
Coverage includes: the local-validation `ValueError`s; the capability-check
`BMDUnsupportedError`s (endpoint not confirmed, camera-reported combination mismatch, and
ambiguous `sensorResolution` across multiple matching entries); the merge-before-write body
(asserting every untouched field from the canned `GET` survives into the `PUT`); both
dual-check paths; that a profile's confirmed `format_names` entry is what makes `"422"`
resolve to `"ProRes:Original"` rather than the derivation rule's (wrong) `"ProRes:422"`;
and — pinning the real-hardware defect above directly — that a codec switch derives
`sensorResolution` from the *matched* `supported_formats()` entry rather than carrying over
whatever the canned pre-write `GET` (standing in for a stale current format) already had.
Two further tests cover the `sensor_resolution` disambiguation parameter directly: it picks
the requested pairing out of several ambiguous matches, and raises `BMDUnsupportedError`
when the camera doesn't pair that exact value with the requested combination at all.

`TestTimelineClipIds` covers the new read verb directly: the capability-check
`BMDUnsupportedError`, a normal parsed readback, and an empty timeline.

Phase 7's writes (`TestEnterExitPlayback`, `TestShuttleAndSeek`, `TestPlayPauseStop`,
`TestSelectClip`) reuse the same fake-`RestClient`/deliver-an-event pattern, plus a new
`make_playback_profile()` helper confirming `TRANSPORT_MODE_PROPERTY`'s and
`PLAYBACK_PROPERTY`'s `put_supported`, and `TIMELINE_PATH`'s `supported` (GET-only, matching
`make_profile_with_record_confirmed()`'s shape for a `NEVER_WRITE` endpoint), and a separate
`make_select_clip_profile()` layering the `codecs`/`resolutions`/`fps_modes`/`format_names`
tables `select_clip()`'s format-switch path needs on top of that. `FakeRestClient` gained
`.delete()`/`.post()` for `select_clip()`'s tests. Coverage includes: the capability-check
`BMDUnsupportedError`s for all four write paths; both dual-check paths for
`enter_playback`/`exit_playback`/`shuttle`/`seek`; that `shuttle()`/`seek()` merge their one
changed field into whatever `_put_playback()`'s initial `GET` returned rather than sending a
bare partial body — `test_shuttle_merges_speed_into_current_body`/
`test_seek_merges_position_into_current_body` assert the full merged body reaches `PUT`,
confirmed via a delivered WS event since `FakeRestClient`'s canned `GET` can't reflect a
write it never actually applied; that `play()`/`pause()`/`stop()` send exactly the bodies
their docstrings claim (`{"speed": 1.0}`/`{"speed": 0.0}`/`{"mode": "InputPreview"}`);
`select_clip()`'s delete-then-post sequencing, its poll-until-*member* behavior (a custom
`client.get` returning a different body each call, the same technique `set_camera_format`'s
own GET-readback tests use), its `BMDVerificationError` on a poll that never matches within
`verify_timeout_s`, that it skips `set_camera_format()` entirely when the camera's format
already matches the clip (asserting zero `PUT`s to `FORMAT_PROPERTY`) but calls it with the
right `(family, variant, resolution, fps)` when it doesn't, and the `ValueError`/
`BMDUnsupportedError` cases for an unknown clip id, a clip missing its codec/videoFormat, an
unparseable `videoFormat`, a codec absent from `format_names`, and a resolution absent from
the profile's `resolutions` table. Two `caplog`-based tests (mirroring the pattern
`tests/unit/test_camera_profile.py` already uses) cover the format-switch logging fix
above: `test_logs_format_mismatch_and_switch_at_info_level` asserts both the mismatch and
the `set_camera_format` `INFO` lines appear when a switch happens,
`test_no_format_switch_log_when_already_matching` asserts neither does when it doesn't.

`tests/unit/tools/rest/test_rest_sweep_camera_format.py` covers
`tools/rest/sweep_camera_format.py` separately, since it's a standalone script rather than
package code — `enumerate_combinations` against real `POCKET_6K_G2`/`POCKET_6K_PRO`
profiles (confirming it does *not* filter `known_unreachable`/`max_fps_int`, unlike the
BLE tool), `expand_with_sensor_resolutions` against a fake session (single match, multiple
sensor-resolution matches, no match at all, and the `format_names`-over-derivation case
again), and `run_combo`'s outcome classification. Loaded via `importlib` under an explicit
module name rather than the plain `sys.path`-insert-and-import pattern
`tests/unit/tools/rest/test_smoke_test_client.py` uses, since this script's filename is
identical to `tools/control/sweep_camera_format.py`'s and a plain `import` would collide
in `sys.modules` across the two test files — see that test file's module docstring.

`list_mount()`/`mount_names()`/`path_exists()` are covered directly in
`test_rest_session.py` (`TestListMount`, `TestMountNames`, `TestPathExists`) with the same
fake-`RestClient` pattern; `tests/unit/rest/test_client.py`'s `TestExists` proves
`exists()` never calls `.json()`/`.text()` at all — a `FakeResponse` with no body
configured would raise if it ever did. `tests/unit/rest/test_media.py` covers
`rest/media.py`'s pure function (`resolve_mount_path`) and its session-composing ones
(`resolve_active_mount`, `stills_marker`, `wait_for_new_still`, `guess_new_still_path`)
against a minimal fake exposing only the `RestCameraSession` methods they call
(`storage_state`, `mount_names`, `list_mount`, `path_exists`) — including that a
same-named `file` entry never counts as the `Stills` directory, that a marker change
appearing mid-poll (not just immediately or never) is detected, and for
`guess_new_still_path`, that it finds a `.braw` match too (not just `.dng`), tries
adjacent minutes, respects a caller-supplied index range, derives the reel from the mount
path rather than needing it passed in separately, and — the real-hardware regression —
prefers the highest matching index when more than one candidate matches the same
timestamp, rather than the first one found in ascending order.

`TestDeviceInfo`/`TestDoformatSupportedFilesystems`/`TestFormatDevice` (Phase 10) cover the
new media-device-formatting surface with the same fake-`RestClient` pattern, plus a new
`make_format_device_profile()` helper confirming `.../doformat`'s `supported` (GET) side only
— deliberately never `put_supported`, mirroring the endpoint's real `NEVER_WRITE` status (see
`format_device()`'s own section above). Coverage includes: `device_info()`'s parsed `state`
and its default-to-`"None"` fallback when the field is missing; `doformat_supported_filesystems()`'s
parsed tuple and its empty-tuple fallback for a non-list body; that omitting `filesystem`
entirely raises `TypeError` before this method's own body ever runs (`filesystem` has no
default — the real-hardware fix); `format_device()`'s `ValueError` when `confirm=False`
(asserting zero requests are sent); its capability-check `BMDUnsupportedError` when the
endpoint isn't profile-confirmed and when a requested `filesystem` isn't in the camera's live
`doformatSupportedFilesystems`; its `BMDVerificationError` when the key `GET` returns no `key`
at all; the full success path (`FakeRestClient.responses` mutated from a background
`asyncio.Task`, the same "deliver later" technique `TestRecordStop`'s polling tests use, to
simulate `state` moving `"Mounted"` -> `"Formatting"` -> a terminal state) asserting the exact
`PUT` body sent with `volume` given explicitly; `test_defaults_volume_from_storage_state_when_not_given`
(the second real-hardware fix), asserting a `volume`-omitting call resolves it from a canned
`/media/workingset`/`/media/active` response matching `device_name` rather than sending the
field bare; `test_raises_value_error_when_device_not_in_storage_and_volume_not_given` and
`test_raises_value_error_when_matching_device_has_no_volume` covering the two cases where that
resolution can't happen at all; and a timeout test where `state` never leaves `"Mounted"` at
all, confirming the `"Formatting"`-must-be-observed-first guard actually gates completion
rather than accepting the very first terminal-looking read.

`TestDeleteClip` (Phase 11) covers `delete_clip()` with a `_delete_clip_client()` helper
supplying everything the method composes through — `clips()` (one clip,
`clip_unique_id=1`), `storage_state()`/`mount_names()` (so `resolve_active_mount()` resolves
unambiguously to a single mount), and an `exists_responses` entry for the real
`/mounts/<mount>/<basename>` path that composition resolves to. Coverage includes: the
`ValueError` when `confirm=False` (asserting zero requests are sent at all, not even
`clips()`); the `ValueError` when `clip_unique_id` isn't found; `BMDVerificationError` when
the resolved path doesn't exist before `DELETE` is ever sent; `BMDVerificationError` when
`exists()` still reports `True` after `DELETE` (a `delete()` call that raised nothing is not
itself proof of deletion — only a fresh read counts); and the full success path, using a
stateful `exists()` override (`True` on the first call, `False` on the second) rather than a
static canned response, since the before/after distinction is the entire point of the
verification being tested — asserting the returned `Clip`, the exact target path `DELETE`
was called with, and that it was called with `api_prefixed=False`. Three more tests, added
alongside the real-hardware `/clips/list`-staleness finding above: a `caplog`-based test
(mirroring `TestSelectClip`'s own format-switch-logging tests) asserting the `WARNING` fires
when a stubbed second `clips()` call still lists the deleted id; a matching negative test
asserting no `WARNING` when it doesn't; and a test confirming a `BMDStorageError` from that
same best-effort check is swallowed rather than propagated, so an already-confirmed deletion
can never be turned into a reported failure by this purely informational follow-up read.
`RestClient.delete()`'s new `api_prefixed` parameter has its own regression test in
`test_client.py`, mirroring `get()`'s existing one for the same `/mounts/...`-is-outside-
`API_BASE` reason.

`TestDeleteStill` covers `delete_still()` with a bare `FakeRestClient` — no `clips()`-style
helper needed, since this method takes its target path directly rather than resolving one.
Coverage mirrors `TestDeleteClip`'s shape wherever the two methods overlap: `ValueError` when
`confirm=False` (asserting zero requests at all, not even `exists()`); `BMDVerificationError`
when the path doesn't exist before `DELETE`; `BMDVerificationError` when it still exists
after; and the full success path via the same stateful-`exists()`-override technique,
asserting the `DELETE` target and its `api_prefixed=False`. No `clip_unique_id`-not-found
case (there's no id to look up) and no `/clips/list`-staleness case (there's no equivalent
listing to check).

`TestDeleteClips` (Phase 13) covers `delete_clips()` with a `_bulk_delete_client()` helper —
`clips()` reporting every requested id, `storage_state()`/`mount_names()` for unambiguous mount
resolution, and a stateful per-path `exists()` that toggles `True` -> `False` independently for
each clip's own target path (so one clip's DELETE confirmation doesn't affect another's).
Coverage: `ValueError` when `confirm=False`; `ValueError` when the id list is empty;
de-duplication (`[1, 1, 2]` deletes each id exactly once); `ValueError` naming every missing id
and sending zero `DELETE`s when any requested id isn't found; the full-success path (all
requested clips returned in `.deleted`, `.failed` empty); a partial-failure path (one clip's
`exists()` never returns `True`, so its `delete_clip()` call raises — confirming the batch still
continues and deletes the clips after it, and that the failure is recorded in `.failed` as an
exact `(clip_unique_id, exception)` pair rather than raised); and a `caplog`-based test
confirming the `ERROR` log line for a failed clip.

`TestDownloadClips` (Phase 15) covers `download_clips()` with a `_bulk_download_client()`
helper — mirrors `_bulk_delete_client()` exactly, using `download_responses` (a per-path byte
payload) in place of `exists_responses`. Coverage: `ValueError` when the id list is empty;
`ValueError` when `dest_dir` doesn't exist — asserting `client.calls == []`, confirming
`dest_dir` is checked before `clips()` is ever called, not just before the first `download()`;
de-duplication (`[1, 1, 2]` downloads each id exactly once); `ValueError` naming every missing
id and downloading zero clips when any requested id isn't found; the full-success path (every
requested clip in `.downloaded` as a `(clip_unique_id, Path)` pair, `.failed` empty);
a partial-failure path (`client.errors[path] = BMDRestError(...)` for one clip, confirming the
batch still continues, downloads the rest, and records the failure in `.failed` rather than
raising); `overwrite` passed through unchanged to every underlying `download_clip()` call; and a
`caplog`-based test confirming the `ERROR` log line for a failed clip.

---

## What's deliberately out of scope

- **No sensor resolution the camera doesn't already offer.** `set_camera_format` sets
  `sensorResolution` (see "Write verbs" above) to whichever value the camera's own
  `supported_formats()` pairs with the requested `(codec, recordResolution, fps)` — the
  optional `sensor_resolution` parameter disambiguates *among* those values when more than
  one exists (real case: `ProRes` at `1920×1080` pairs with three;
  `tools/rest/sweep_camera_format.py` is what exercises every one of them), but it cannot
  ask for a sensor resolution the camera doesn't already pair with that combination —
  that still raises `BMDUnsupportedError`. Whether `PUT /system/format` can set a sensor
  resolution *other than* one already paired with the requested codec/resolution/fps (the
  original "Sensor Area" selector question, docs/rest/transport.md) remains a separate,
  still-open question this method doesn't attempt to answer. `select_clip()` inherits this
  gap directly and has no `sensor_resolution` parameter of its own to work around it —
  confirmed real, `POCKET_6K_G2 v8.6`, 2026-08-04: `select_clip()` on a clip whose own
  `ProRes:Proxy @ 1920x1080p23.98` format is exactly this ambiguous combination raised the
  same `BMDUnsupportedError`, since `Clip` (from `clips()`) carries no `sensorResolution`
  field to disambiguate with even if `select_clip()` wanted to pass one through. A clip
  recorded at one of this ambiguous combination's resolutions cannot be selected via
  `select_clip()` alone unless the camera already happens to be at a matching format —
  deliberately: an unsupported operation should raise immediately, not guess (design
  principle 7). `examples/rest_playback.py` composes a retry around that strict boundary
  instead of weakening it: `_select_clip_trying_all_sensor_resolutions()` (added
  2026-08-05) catches the `BMDUnsupportedError`, independently re-derives the real
  candidate `sensorResolution` values from `supported_formats()`, and tries
  `set_camera_format()` with each in turn — `select_clip()`'s own format comparison never
  checks `sensorResolution`, so once a candidate is set, a second `select_clip()` call sees
  the format as already matching and proceeds normally. Example-level, not library-level,
  on purpose — the library still raises loudly on the first attempt; only the caller that
  chooses to retry pays for the extra requests. **Real-hardware-confirmed, `POCKET_6K_G2
  v8.6`, 2026-08-05**: `clip_unique_id=1` (`ProRes:HQ @ 1920x1080p25`, the same three-way
  ambiguous combination) — the ambiguity error fired as expected, the retry found the same
  3 candidates, and the *first* one tried (`sensor_resolution=(2880, 1512)`) succeeded on
  the first attempt; the full remaining `rest_playback.py` sequence (`enter_playback`
  through `exit_playback`) then passed clean. See `select_clip()`'s docstring, finding #5,
  for why this run also closes that method's own remaining open branch.
- **No storage precondition on `record_stop()`.** Only `record_start()` checks
  `storage_state()` — matching CLAUDE.md design principle 10's wording ("before recording
  or photo capture"), which names starting an operation, not ending one.
- **No caching of plain `GET`-backed read verbs.** `get_format()`, `storage_state()`,
  `clips()`, `timecode()`, `supported_formats()` all hit the camera fresh on every call —
  this hasn't changed. `CameraState` (Phase 9) exists only for the notification-driven
  subset (`is_recording`, `last_known_storage`, `playback_interrupted`, `last_known_play`/
  `last_known_stop`) — the fields that were already tracked continuously before the
  refactor, not a general cache over the read surface.
- **No dedicated `/transports/0/play`/`/transports/0/stop` support.** `play()`/`stop()`
  are aliases (`shuttle(1.0)`/`exit_playback()`) rather than wrappers around these two
  endpoints — see "`play()`/`pause()`/`stop()`" above for why. If a real hardware run shows
  the dedicated triggers behave meaningfully differently, this is the first place to
  revisit.
- **No `type`/`loop`/`singleClip` parameters.** `/transports/0/playback`'s real body
  carries `type`/`loop`/`singleClip` fields beyond `speed`/`position` (see
  "`shuttle(speed)` / `seek(position)`" above); this session doesn't expose them yet, and
  every write preserves whatever the preceding `GET` reported.
- **No caller-curated multi-clip playlist.** This isn't an unimplemented feature so much
  as a discovered camera limitation — see `select_clip()`'s section above. There's no
  `set_timeline(clip_unique_ids)` because there's nothing on this camera for it to mean;
  the playable set is always every clip sharing the current format, one format group at a
  time.
- **No bulk still deletion, and no "delete by criteria."** `delete_clips()` (Phase 13, above)
  covers explicit-list bulk deletion for clips only — `delete_still()` remains single-target
  (stills have no `clips()`-equivalent listing to validate a batch against in the first place).
  Neither takes a filter or predicate: there is no "delete all clips older than X" or similar
  convenience wrapper, deliberately — every clip a caller wants deleted must be named by its own
  `clip_unique_id`, explicitly, never inferred.
- **No bulk still download, and no "download by criteria," for the identical reason.**
  `download_clips()` (Phase 15, above) covers explicit-list bulk download for clips only —
  `download_still()` remains single-target, same gap `delete_still()` already has. No filter or
  predicate either: every clip a caller wants downloaded must be named by its own
  `clip_unique_id`, explicitly, never inferred.
