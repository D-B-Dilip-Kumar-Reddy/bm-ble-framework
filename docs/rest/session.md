# REST Session — State and Control Surface (Phases 3-5)

**Status:** read verbs (Phase 3) and `record_start`/`record_stop` (Phase 4) are
implemented and **real-hardware-confirmed** — `POCKET_6K_G2` and `POCKET_6K_PRO v8.6`,
2026-08-03, `record_start`/`record_stop` each 6/6 via the dual-check
(`examples/rest_record_start_stop.py`), the same run that also caught and fixed
`wait_while_recording`'s return-value-inversion defect (see below). `set_camera_format`
(Phase 5) is implemented, unit-tested, and **partially real-hardware-confirmed**: a
`ProRes/4K DCI` write — the exact combination BLE's write path cannot reach
(`known_unreachable`) — was confirmed on the first `examples/rest_change_format.py` run,
`POCKET_6K_G2 v8.6`, 2026-08-03. The very next step in that same run (`BRAW/4K DCI`)
exposed a real defect (`sensorResolution` derivation, see below) that has since been
fixed; a full round-trip confirming the fix itself is still open.

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
`set_camera_format`, mirroring `examples/change_codec.py`.

Every read verb (`get_format`, `supported_formats`, `storage_state`, `clips`, `timecode`)
is a plain `GET`, fetched fresh on every call — there is no background cache to go stale.
`is_recording` is the one exception: it is tracked continuously from
`/transports/0/record` `propertyValueChanged` events, mirroring the BLE `CameraSession`'s
`is_recording` attribute (notification-derived only, never inferred from a request this
session itself made — design principle 4).

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

### `timecode()` → `Timecode`

`GET /transports/0/timecode`, decoded via `rest/timecode.py`'s `decode_rest_timecode` —
see "Timecode decode" below. Reuses the BLE `Timecode` dataclass and `duration_seconds()`
as-is; only the wire decode differs between transports.

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

---

## Write verbs (Phases 4-5)

### `record_start()` / `record_stop()`

`PUT /transports/0/record {"recording": bool}`, verified via the dual-check design
principle 3 specifies for REST: a WS `propertyValueChanged` event on `/transports/0/record`
primary, a `GET /transports/0/record` readback secondary. `204` on the `PUT` means
accepted, not applied — if neither channel confirms the requested state within
`verify_timeout_s` (constructor parameter, default `5.0`, distinct from `timeout_s`'s
single-request timeout and `ws_timeout_s`'s WS connect timeout), `BMDVerificationError` is
raised.

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
than guessing — see `_resolve_supported_format`'s docstring. This is **not** the same thing
as exposing arbitrary `sensorResolution` selection (still out of scope, see below) — it's
making sure every write this method already attempts is internally consistent with what
the camera itself reports pairing together.

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
passed; the rejection came from the camera itself, on the `PUT`). Fixed as described above;
a full round-trip confirming the fix itself on real hardware is still open.

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

---

## What's deliberately out of scope

- **No arbitrary `sensorResolution` selection.** `set_camera_format` *does* set
  `sensorResolution` now (see "Write verbs" above) — but only to whichever value the
  camera's own `supported_formats()` pairs with the requested `(codec, recordResolution,
  fps)`, and only ever the one such value when it's unambiguous. A caller cannot ask for a
  *specific* sensor resolution among several a camera offers for the same
  `(codec, recordResolution, fps)` (real case: `ProRes` at `1920×1080`) — that raises
  `BMDUnsupportedError` today rather than picking one. Exposing a `sensor_resolution`
  parameter to disambiguate is future work, not something this method invents an answer
  for. Whether `PUT /system/format` can set a sensor resolution *other than* one already
  paired with the requested codec/resolution/fps (the "Sensor Area" selector question,
  docs/rest/transport.md) remains a separate, still-open question this method doesn't
  attempt to answer.
- **No storage precondition on `record_stop()`.** Only `record_start()` checks
  `storage_state()` — matching CLAUDE.md design principle 10's wording ("before recording
  or photo capture"), which names starting an operation, not ending one.
- **No caching.** Every read verb hits the camera fresh. A future phase may add a
  `CameraState`-style cached view if a real use case needs one; nothing here assumes it
  will.
