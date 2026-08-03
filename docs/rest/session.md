# REST Session — Read-Only State Surface (Phase 3)

**Status:** implemented — `RestCameraSession` is live and confirmed against real
`POCKET_6K_PRO v8.6` hardware. Read verbs only; no write orchestration yet (Phase 4/5).

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
```

`examples/rest_read_state.py` is the first (and currently only) consumer.

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
session lifetime with no extra call needed. `wait_while_recording(timeout)` blocks until
`is_recording` becomes `False`, returning `True` if it was already stopped/unknown or
stops within the timeout, `False` if it times out while still recording.

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

## Codec name mapping (`rest/mapping.py`)

`RestCameraSession` (and, later, its write orchestration) takes profile *names* — the
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

---

## What's deliberately out of scope

- **No writes.** `RestCameraSession` has no `set_*`/`put`-shaped method yet — that is
  Phase 4 (record start/stop) and Phase 5 (format writes), which will reuse
  `RestEventRouter.arm()`/`wait_for()` exactly as `tools/rest/smoke_test_client.py`'s
  `--verify-write` mode already proved works end to end on real hardware.
- **No storage preconditions.** Design principle 10's pre-condition gate ("verify storage
  is ready before recording, raise `BMDStorageError` otherwise") has nothing to gate yet
  with no write path — it lands with Phase 4.
- **No caching.** Every read verb hits the camera fresh. A future phase may add a
  `CameraState`-style cached view if a real use case needs one; nothing here assumes it
  will.
