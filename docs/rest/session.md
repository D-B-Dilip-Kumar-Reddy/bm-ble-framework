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
   throughout. This matters for `_require_storage_ready()`, which gates `record_start()` on
   `remaining_record_time > 0`, and for any future threshold built on that field — read it
   as a pre-switch leftover, not a current number, until a recording has started.

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
storage device, or the active device reports `remaining_record_time <= 0`. `record_stop()`
carries no such check and is a no-op when `is_recording` already positively confirms the
camera isn't recording, mirroring the BLE `CameraSession.record_stop`'s documented
no-echo-on-redundant-write handling (`docs/ble/recording.md`) — whether this camera's REST
endpoint behaves the same way on a redundant `PUT` is unconfirmed, but the guard is
harmless either way since `is_recording` is only ever notification-derived (design
principle 4), never assumed.

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
entries" observation itself still stands; the causal reading of *why* does not, until a
run tests a format switch with no `POST` at all.

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
alone already having done it. The "no stale entries" result stands regardless.)*

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
negative result is the first evidence pointing at the second reading. Not yet tested
directly (switch format with zero `POST` at all, confirm the timeline populates from the
switch alone) — the honest next step to fully settle whether `POST /timelines/0/add` ever
does anything on this firmware, versus being pure formality riding on a switch that would
have populated the timeline by itself either way.

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
  chooses to retry pays for the extra requests. Not yet run against real hardware.
- **No storage precondition on `record_stop()`.** Only `record_start()` checks
  `storage_state()` — matching CLAUDE.md design principle 10's wording ("before recording
  or photo capture"), which names starting an operation, not ending one.
- **No caching.** Every read verb hits the camera fresh. A future phase may add a
  `CameraState`-style cached view if a real use case needs one; nothing here assumes it
  will.
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
