"""
bmd_camera/rest/session.py
=============================
RestCameraSession — read-only REST state surface (Phase 3). Composes
`RestClient` (transport) and `RestEventRouter` (WS event buffering) exactly
as `ble/session.py`'s `CameraSession` composes `BMDCameraController` and
`NotificationRouter` — see design principle 5's transport/protocol
boundary, held here for REST.

STATUS: read verbs, record start/stop (Phase 4), format writes (Phase 5), photo
confirmation primitives (Phase 6), and playback/gallery writes (Phase 7) — the
REST dual-check design principle 3 has always specified: a WS
`propertyValueChanged` event primary, a `GET` readback secondary. Phase 7's
entire sequence — `select_clip()` -> `enter_playback()` -> `play()` ->
`pause()` -> `seek()` -> `shuttle()` (forward and backward) -> `stop()` ->
`exit_playback()` — is real-hardware-confirmed end to end on both
`POCKET_6K_G2` and `POCKET_6K_PRO v8.6` (2026-08-04, `examples/rest_playback.py`,
every step's own dual-check passing on both cameras). `select_clip()` itself
replaces an earlier `set_timeline(clip_unique_ids: list[int])` design that
real hardware disproved outright — this camera has no concept of a
caller-curated playlist; the playable set is always every clip matching the
camera's current format, confirmed on real hardware two independent ways (a
REST readback and the camera's own on-screen playback view both agreeing on
the same seven-clip group) — see `select_clip()`'s own docstring for the
full real-hardware debugging trail.

    async with RestCameraSession("172.27.97.141", "POCKET_6K_PRO", "v8.6") as session:
        fmt = await session.get_format()
        storage = await session.storage_state()
        clips = await session.clips()
        tc = await session.timecode()
        await session.record_start()
        await session.record_stop()
        await session.set_camera_format("BRAW", "5:1", "4K DCI", "23.98")
        await session.select_clip(clips[0].clip_unique_id)
        await session.enter_playback()
        await session.play()
        await session.exit_playback()

`is_recording` is the one piece of state this session tracks continuously
rather than fetching on demand — notification-derived only, from
`/transports/0/record` `propertyValueChanged` events (design principle 4),
mirroring the BLE `CameraSession`'s `is_recording` attribute exactly. This
holds even for `record_start`/`record_stop`'s own writes: their verification
reads the event (and, secondarily, a `GET`) locally rather than setting
`is_recording` directly, so a caller relying on `is_recording` still only
ever sees a value the camera itself reported. Everything else (`get_format`,
`storage_state`, `clips`, `timecode`) is a plain `GET`, fetched fresh on
every call — there is no background cache to go stale.

`playback_interrupted`/`wait_for_playback_interrupt()` (Phase 8 item 2, part
2) hold the same discipline for playback that `is_recording` holds for
recording — set only from a `/transports/0/playback` speed deviating from
`_expected_speed` (the speed this session itself last set, not a fixed `0`
check) or a `/transports/0` mode-left-`"Output"` event that arrived with
none of this session's own writes in flight, never inferred from a
`pause()`/`stop()` call this session itself made. Real-hardware-confirmed
end to end, `POCKET_6K_G2 v8.6`, 2026-08-05 (`tools/rest/
verify_playback_interrupt.py`): the sanity phase found and fixed a
false-positive in the in-flight guard (see `_on_event`'s docstring), and a
following run confirmed the actual camera-initiated case — a real
interrupt mid-`play()`, detected in 13.5s.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover - exercised only without the dependency
    aiohttp = None  # type: ignore[assignment]

from ..camera_profile import CameraProfile
from ..exceptions import (
    BMDConnectionError,
    BMDStorageError,
    BMDUnsupportedError,
    BMDVerificationError,
)
from .client import RestClient, require_aiohttp
from .constants import MOUNTS_PATH, WS_PATH
from .events import RestEventRouter
from .exceptions import BMDRestError
from .mapping import resolve_ble_codec_name, resolve_rest_codec_name
from .media import resolve_active_mount
from .state import CameraState, StorageDevice, StorageState
from .timecode import Timecode, decode_rest_timecode

logger = logging.getLogger(__name__)

RECORD_PROPERTY = "/transports/0/record"
FORMAT_PROPERTY = "/system/format"
TRANSPORT_MODE_PROPERTY = "/transports/0"
PLAYBACK_PROPERTY = "/transports/0/playback"
WORKINGSET_PROPERTY = "/media/workingset"
PLAY_PROPERTY = "/transports/0/play"
STOP_PROPERTY = "/transports/0/stop"
TIMELINE_PATH = "/timelines/0"
TIMELINE_ADD_PATH = "/timelines/0/add"

# How often _set_recording_state() re-polls the secondary GET readback once
# the primary WS wait has used its slice of the budget — see record_stop's
# widened timeout below.
RECORD_POLL_INTERVAL_S = 0.5


def _websocket_url(base_url: str) -> str:
    if base_url.startswith("https://"):
        return f"wss://{base_url[len('https://') :]}{WS_PATH}"
    if base_url.startswith("http://"):
        return f"ws://{base_url[len('http://') :]}{WS_PATH}"
    raise ValueError(f"base_url must start with http:// or https:// — got {base_url!r}")


# ── Read-verb result types ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Format:
    """`GET /system/format`'s body — the REST analogue of the BLE profile's
    `codecs`/`resolutions`/`fps_modes` tables, but reported directly by the
    camera rather than looked up. `codec` and `frame_rate` are REST's own
    spelling (see `mapping.py` for translating to/from the BLE profile's
    vocabulary)."""

    codec: str
    frame_rate: str
    record_resolution: tuple[int, int]  # (width, height)
    sensor_resolution: tuple[int, int]
    off_speed_enabled: bool
    off_speed_frame_rate: int | None = None
    min_off_speed_frame_rate: int | None = None
    max_off_speed_frame_rate: int | None = None


@dataclass(frozen=True)
class SupportedFormat:
    """One entry of `GET /system/supportedFormats`'s `supportedFormats`
    array — a (record resolution, sensor resolution) combination and the
    codecs/frame rates the camera itself reports supporting there. Makes
    the BLE profile's hand-maintained `resolutions`/`codecs` tables
    redundant on the REST path (docs/rest/transport.md)."""

    codecs: tuple[str, ...]
    frame_rates: tuple[str, ...]
    record_resolution: tuple[int, int]
    sensor_resolution: tuple[int, int]
    min_off_speed_frame_rate: int | None = None
    max_off_speed_frame_rate: int | None = None


@dataclass(frozen=True)
class Clip:
    """One entry of `GET /clips/list`'s `clipList` array — note the real
    key is `clipList`, not `clips` as the field's own name might suggest
    (docs/rest/transport.md). No stills appear here and there is no file
    size field — the photo-capture confirmation design (Phase 6) cannot
    lean on this endpoint."""

    clip_unique_id: int
    file_path: str
    codec: str | None
    container: str | None
    start_timecode: str | None
    duration_timecode: str | None
    video_format: str | None


@dataclass(frozen=True)
class RecordingResult:
    """`confirm_new_clip()`'s return value (Phase 9) — a one-shot
    verification result, not continuously-tracked state, so it lives here
    with `Clip`/`Format`/`SupportedFormat` rather than on `CameraState`.
    `bytes_written` is `None` whenever it couldn't be computed (see
    `confirm_new_clip()`'s own docstring) — never a guessed or defaulted
    number."""

    clip: Clip
    bytes_written: int | None


@dataclass(frozen=True)
class DeviceInfo:
    """`GET /media/devices/{deviceName}`'s body (Phase 10) — per the
    official BMD REST spec (`MediaControl.yaml`), `state` is one of
    `"None"`, `"Scanning"`, `"Mounted"`, `"Uninitialised"`, `"Formatting"`,
    `"RaidComponent"`. Kept as a plain `str`, not a Python enum — the spec
    is the source of truth for the exact allowed values, and validating
    against a hardcoded set here would just be another way to be wrong
    about a value this codebase hasn't independently confirmed on real
    hardware yet (design principle 6). Not yet real-hardware-confirmed —
    this dataclass's shape comes from the spec, not a sweep."""

    state: str


# ── Parsing helpers ──────────────────────────────────────────────────────────


def _recording_flag(value: Any) -> bool | None:
    """Extract `/transports/0/record`'s `{"recording": bool}` shape —
    `Notification.yaml`'s documented event `value`, shared by the `GET`
    readback body too. Real-hardware-confirmed, `POCKET_6K_G2` and
    `POCKET_6K_PRO v8.6`, 2026-08-03: `record_start`/`record_stop` each
    confirmed 6/6 via this shape on at least one of the two channels every
    time — see docs/rest/session.md. Returns None for anything else,
    including a `wait_for()` timeout's own `None`, so a caller can treat
    "malformed" and "no delivery yet" identically — both mean "not
    confirmed by this channel"."""
    if isinstance(value, dict) and isinstance(value.get("recording"), bool):
        return value["recording"]
    return None


def _format_matches(
    value: Any,
    *,
    codec: str,
    frame_rate: str,
    resolution: tuple[int, int],
    sensor_resolution: tuple[int, int],
) -> bool:
    """Whether `/system/format`'s event `value` or `GET` readback body
    reports exactly the requested `codec`/`frameRate`/`recordResolution`/
    `sensorResolution` — the shape `Format`/`_parse_format` already parses.
    `sensor_resolution` is included because it's a real dependent field, not
    an independent one — see `set_camera_format`'s docstring for the
    real-hardware defect that assuming otherwise caused. Returns False for
    anything malformed, mirroring `_recording_flag`'s "not confirmed by
    this channel" treatment of a bad or missing value."""
    if not isinstance(value, dict):
        return False
    if value.get("codec") != codec or value.get("frameRate") != frame_rate:
        return False
    if _resolution(value.get("recordResolution")) != resolution:
        return False
    return _resolution(value.get("sensorResolution")) == sensor_resolution


def _resolution(body: dict | None) -> tuple[int, int]:
    if not body:
        return (0, 0)
    return (body.get("width", 0), body.get("height", 0))


def _parse_format(body: dict) -> Format:
    return Format(
        codec=body["codec"],
        frame_rate=body["frameRate"],
        record_resolution=_resolution(body.get("recordResolution")),
        sensor_resolution=_resolution(body.get("sensorResolution")),
        off_speed_enabled=body.get("offSpeedEnabled", False),
        off_speed_frame_rate=body.get("offSpeedFrameRate"),
        min_off_speed_frame_rate=body.get("minOffSpeedFrameRate"),
        max_off_speed_frame_rate=body.get("maxOffSpeedFrameRate"),
    )


def _parse_supported_format(entry: dict) -> SupportedFormat:
    return SupportedFormat(
        codecs=tuple(entry.get("codecs", ())),
        frame_rates=tuple(entry.get("frameRates", ())),
        record_resolution=_resolution(entry.get("recordResolution")),
        sensor_resolution=_resolution(entry.get("sensorResolution")),
        min_off_speed_frame_rate=entry.get("minOffSpeedFrameRate"),
        max_off_speed_frame_rate=entry.get("maxOffSpeedFrameRate"),
    )


def _parse_storage_device(entry: dict) -> StorageDevice:
    return StorageDevice(
        index=entry["index"],
        device_name=entry.get("deviceName", ""),
        active=entry.get("activeDisk", False),
        total_space=entry.get("totalSpace", 0),
        remaining_space=entry.get("remainingSpace", 0),
        remaining_record_time=entry.get("remainingRecordTime", 0),
        clip_count=entry.get("clipCount", 0),
        volume=entry.get("volume"),
    )


def _parse_storage_state(workingset_body: dict, active_body: dict | None) -> StorageState:
    devices = tuple(_parse_storage_device(entry) for entry in workingset_body.get("workingset", ()))
    active_index = active_body.get("workingsetIndex") if active_body else None
    active_device = next((d for d in devices if d.index == active_index), None)
    if active_device is None:
        active_device = next((d for d in devices if d.active), None)
    return StorageState(devices=devices, active_device=active_device)


def _parse_clip(entry: dict) -> Clip:
    codec_format = entry.get("codecFormat") or {}
    return Clip(
        clip_unique_id=entry["clipUniqueId"],
        file_path=entry.get("filePath", ""),
        codec=codec_format.get("codec"),
        container=codec_format.get("container"),
        start_timecode=entry.get("startTimecode"),
        duration_timecode=entry.get("durationTimecode"),
        video_format=entry.get("videoFormat"),
    )


def _parse_device_info(body: dict) -> DeviceInfo:
    return DeviceInfo(state=body.get("state", "None"))


def _transport_mode(value: Any) -> str | None:
    """Extract `/transports/0`'s `{"mode": "InputPreview"|"InputRecord"|
    "Output"}` shape — confirmed real by `tools/rest/probe_endpoints.py`'s
    write catalog, which reshapes a `GET /transports/0` body by echoing
    back exactly this field (`docs/rest/transport.md`'s reshaping table)."""
    if isinstance(value, dict) and isinstance(value.get("mode"), str):
        return value["mode"]
    return None


def _contains(value: Any, expected: dict[str, Any]) -> bool:
    """Whether `value` (a WS event value or a `GET` readback body) is a
    dict containing every key/value pair in `expected`. A generic
    structural dual-check, used only for `/transports/0/playback` (Phase
    7) — contrast `_recording_flag`/`_format_matches`, which parse named,
    sweep-confirmed fields. `/transports/0/playback`'s own body has never
    been independently captured on real hardware (docs/rest/session.md);
    `expected`'s keys are this migration's plan-derived hypothesis, not a
    confirmed sample. A field-name mismatch makes this correctly return
    `False` — `shuttle()`/`seek()` then raise `BMDVerificationError` rather
    than reporting a success this method cannot actually attest to."""
    return isinstance(value, dict) and all(value.get(k) == v for k, v in expected.items())


def _parse_timeline_clip_ids(body: Any) -> list[int]:
    """Extraction of the clip ids `GET /timelines/0` reports. Real shape
    confirmed, `POCKET_6K_PRO v8.6`, 2026-08-04 (operator Postman
    debugging): `{"clips": [{"clipUniqueId": int, "frameCount": int}]}` —
    a dict-list under `"clips"`, matching `/clips/list`'s own
    `{"clipUniqueId": ...}` convention rather than a flat id list; the
    extra `"frameCount"` field is simply ignored here. The flat-int-list
    branch below predates that confirmation and is kept only as a
    defensive fallback in case a future firmware reports it that way — it
    has never actually been observed on real hardware."""
    if not isinstance(body, dict):
        return []
    entries = body.get("clips")
    if not isinstance(entries, list):
        return []
    ids: list[int] = []
    for entry in entries:
        if isinstance(entry, int):
            ids.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("clipUniqueId"), int):
            ids.append(entry["clipUniqueId"])
    return ids


_VIDEO_FORMAT_RE = re.compile(r"^(\d+)x(\d+)p(\d+(?:\.\d+)?)$")


def _parse_video_format(video_format: str) -> tuple[int, int, str] | None:
    """Decode `Clip.video_format`'s real wire shape (`GET /clips/list`,
    confirmed `POCKET_6K_PRO v8.6`, 2026-08-04: `"4096x2160p24"`,
    `"4096x2160p23.98"`, `"3840x2160p60"`, `"6144x2560p60"`) into
    `(width, height, fps_str)`. `fps_str` is returned exactly as printed —
    `"23.98"` vs `"24"` matters, since it's compared directly against
    `Format.frame_rate` and looked up directly in `fps_modes` (no
    translation needed there — `mapping.py`'s module docstring). Returns
    `None` for anything that doesn't match this shape, rather than raising
    — `select_clip()` turns that into a loud `BMDUnsupportedError` naming
    the clip, not a stack trace from a regex match on `None`."""
    match = _VIDEO_FORMAT_RE.match(video_format)
    if match is None:
        return None
    width, height, fps_str = match.groups()
    return int(width), int(height), fps_str


def _resolution_name_for_dimensions(
    resolutions: dict[str, Any], width: int, height: int
) -> str | None:
    """Reverse lookup into the (BLE, shared — design principle 1)
    `resolutions` profile table: the name whose `(width, height)` matches,
    or `None` if no entry does. The forward direction (`"4K DCI"` ->
    `{width: 4096, ...}`) is what `set_camera_format` already uses;
    `select_clip()` needs the reverse to turn a clip's REST-reported pixel
    dimensions back into a profile name `set_camera_format` accepts."""
    for name, spec in resolutions.items():
        if spec.width == width and spec.height == height:
            return name
    return None


# ── Session ──────────────────────────────────────────────────────────────────


class RestCameraSession:
    """Async context manager over one camera's REST + WebSocket surface."""

    def __init__(
        self,
        host: str,
        model_key: str,
        firmware: str,
        *,
        scheme: str = "http",
        port: int | None = None,
        timeout_s: float = 5.0,
        ws_timeout_s: float = 5.0,
        verify_timeout_s: float = 5.0,
        stop_verify_timeout_s: float | None = None,
        session: Any | None = None,
    ) -> None:
        require_aiohttp()
        self.profile = CameraProfile.for_model(model_key=model_key, firmware=firmware)
        self.host = host
        self.scheme = scheme
        self.port = port
        self.timeout_s = timeout_s
        self.ws_timeout_s = ws_timeout_s
        # Timeout for a write's dual-check verification (design principle 3's
        # REST sibling) — how long to wait for the WS event before falling
        # back to a GET readback. Distinct from timeout_s (one HTTP request)
        # and ws_timeout_s (the WS connect handshake).
        self.verify_timeout_s = verify_timeout_s
        # record_stop's overall verification budget — wider than
        # verify_timeout_s because closing the .braw and writing its index is
        # I/O-bound and real-hardware-confirmed slower than every other
        # write's confirmation (docs/rest/session.md, 2026-08-05: PUT
        # /transports/0/record itself took ~1.1-1.2s to return on 3 separate
        # runs, against record_start's ~2ms). Defaults to 3x verify_timeout_s
        # rather than sharing its budget with the fast writes.
        self.stop_verify_timeout_s = (
            stop_verify_timeout_s if stop_verify_timeout_s is not None else verify_timeout_s * 3
        )
        self._log = logging.getLogger(f"{__name__}.{model_key}")

        # `session` may be injected (real or fake) for testing, mirroring
        # RestClient's own constructor — see tests/unit/rest/test_session.py.
        self._session: Any | None = session
        self._owns_session = session is None
        self._client: RestClient | None = None
        self._router = RestEventRouter(on_event=self._on_event)

        # `CameraState` (Phase 9) — every notification-driven field this
        # session tracks continuously rather than fetching on demand,
        # updated only from a real WS propertyValueChanged event (design
        # principle 4), never inferred from a request this session itself
        # made. See state.py's own module docstring for the full rationale;
        # the properties immediately below expose each field under its
        # original attribute name, so nothing outside this class needs to
        # know `self.state` exists at all.
        self.state = CameraState()
        self._recording_stopped = asyncio.Event()

        # wait_for_low_storage() arms _low_storage_min_* and waits on
        # _low_storage_event, which _on_event() sets when a pushed
        # last_known_storage value crosses the currently armed threshold.
        # Ephemeral per-call arming, not camera state — stays here, not on
        # CameraState.
        self._low_storage_min_record_time_s: float | None = None
        self._low_storage_min_space_bytes: int | None = None
        self._low_storage_event = asyncio.Event()

        # `_playback_write_in_flight`/`_transport_mode_write_in_flight` are
        # the arm-adjacent guards _put_playback()/_set_transport_mode() hold
        # True for the duration of their own dual-check — and,
        # real-hardware-confirmed (POCKET_6K_G2 v8.6, 2026-08-05), _on_event
        # checks *both* flags in *both* interrupt branches, not just the one
        # matching a write's own property, since leaving "Output" mode was
        # found to also push a side-effect PLAYBACK_PROPERTY speed=0 event a
        # single-flag guard missed. See _on_event's own docstring for the
        # full finding. `_expected_speed` is the speed value this session's
        # own last confirmed enter_playback()/shuttle()/play()/pause() call
        # actually set — _on_event flags any pushed speed that differs from
        # it (not only a drop to 0), so a camera-initiated speed change that
        # lands somewhere other than 0 is caught too. Set to 0.0 by
        # enter_playback() (the camera opens paused) and updated by
        # _put_playback() after each confirmed speed-changing write. All
        # three are this session's own write-tracking bookkeeping, not
        # camera-reported state — stay here, not on CameraState.
        self._playback_write_in_flight = False
        self._transport_mode_write_in_flight = False
        self._expected_speed: float | None = None

    @property
    def base_url(self) -> str:
        netloc = f"{self.host}:{self.port}" if self.port else self.host
        return f"{self.scheme}://{netloc}"

    # ── CameraState property delegation (Phase 9) ──────────────────────────
    # Every field CameraState holds, exposed under its pre-refactor
    # attribute name — external code (examples, tools, tests) is unaffected
    # by `self.state` existing at all. See state.py's module docstring.

    @property
    def is_recording(self) -> bool | None:
        return self.state.is_recording

    @is_recording.setter
    def is_recording(self, value: bool | None) -> None:
        self.state.is_recording = value

    @property
    def last_known_storage(self) -> StorageState | None:
        return self.state.last_known_storage

    @last_known_storage.setter
    def last_known_storage(self, value: StorageState | None) -> None:
        self.state.last_known_storage = value

    @property
    def _in_playback(self) -> bool:
        return self.state._in_playback

    @_in_playback.setter
    def _in_playback(self, value: bool) -> None:
        self.state._in_playback = value

    @property
    def last_known_play(self) -> bool | None:
        return self.state.last_known_play

    @last_known_play.setter
    def last_known_play(self, value: bool | None) -> None:
        self.state.last_known_play = value

    @property
    def last_known_stop(self) -> bool | None:
        return self.state.last_known_stop

    @last_known_stop.setter
    def last_known_stop(self, value: bool | None) -> None:
        self.state.last_known_stop = value

    @property
    def playback_interrupted(self) -> asyncio.Event:
        """No setter — a mutable `asyncio.Event`, mutated via its own
        `.set()`/`.clear()`, never reassigned. `CameraState()`'s
        `default_factory=asyncio.Event` gives every session its own Event
        instance; `enter_playback()` clears it, `_on_event` sets it."""
        return self.state.playback_interrupted

    async def __aenter__(self) -> RestCameraSession:
        """Real-hardware-confirmed failure mode this guards against
        (`POCKET_6K_G2 v8.6`, 2026-08-03): if the WS connect fails (e.g. the
        host doesn't resolve), `__aenter__` never returns, so Python never
        calls `__aexit__` — an `aiohttp.ClientSession` opened here would
        otherwise leak (`ERROR - Unclosed client session`) rather than being
        closed. `connected` tracks whether every step succeeded; `finally`
        closes an owned session on any failure path without catching or
        masking the original exception.

        Also subscribes to `/system/format` here, alongside
        `/transports/0/record` — a second real-hardware-confirmed defect
        this fixes (`POCKET_6K_G2 v8.6`, 2026-08-03,
        `tools/rest/sweep_camera_format.py`'s first run): `set_camera_format`
        arms and waits on `FORMAT_PROPERTY` for its dual-check's primary
        channel, but nothing had ever subscribed the router to it, so the
        camera never had a reason to push `/system/format` events to this
        connection at all. Every one of 544 real writes in that run
        therefore burned the *entire* `verify_timeout_s` (uniformly ~6.0s
        each, not the fast primary-channel case) before falling through to
        the secondary `GET` readback, which is what actually confirmed each
        one — the writes were never wrong, but the "primary" channel was
        structurally dead weight for the whole run. Subscribing here, once,
        for the session's lifetime, gives `set_camera_format` the same
        standing subscription `record_start`/`record_stop` already had for
        `/transports/0/record`.

        Also subscribes to `/media/workingset`, confirmed real and pushing
        live values (`POCKET_6K_G2 v8.6`, 2026-08-05, `tools/rest/watch_events.py`)
        — the primary channel `wait_for_low_storage()` depends on. Without
        this the property would never push to this connection at all, the
        same gap `/system/format` had before this fix.

        Also subscribes to `/transports/0/play` and `/transports/0/stop`
        (Phase 8 item 2) — confirmed real and pushing correctly-computed
        booleans that track `/transports/0/playback`'s `speed` field
        precisely (`POCKET_6K_G2 v8.6`, 2026-08-05,
        `tools/rest/watch_events.py` run alongside `examples/rest_playback.py`
        — see `docs/rest/session.md`'s `play()`/`pause()`/`stop()` section).
        Prior to this fix these two were the same kind of dead channel
        `/system/format` and `/media/workingset` were before their own
        fixes above: real and observed pushing correctly by a caller that
        subscribed independently, but never subscribed by this session
        itself, so `last_known_play`/`last_known_stop` would otherwise never
        update. `playback_interrupted`'s own detection does not depend on
        either of these two (see `_on_event`'s docstring for why
        `/transports/0/playback`'s own `speed` field is the interrupt
        trigger instead) — they are tracked here purely as a second,
        independent corroborating signal.
        """
        if self._session is None:
            self._session = aiohttp.ClientSession()
        connected = False
        try:
            self._client = RestClient(
                self.host,
                scheme=self.scheme,
                port=self.port,
                timeout_s=self.timeout_s,
                session=self._session,
            )
            await self._router.connect(
                self._session, _websocket_url(self.base_url), timeout_s=self.ws_timeout_s
            )
            await self._router.subscribe(RECORD_PROPERTY)
            await self._router.subscribe(FORMAT_PROPERTY)
            await self._router.subscribe(TRANSPORT_MODE_PROPERTY)
            await self._router.subscribe(PLAYBACK_PROPERTY)
            await self._router.subscribe(WORKINGSET_PROPERTY)
            await self._router.subscribe(PLAY_PROPERTY)
            await self._router.subscribe(STOP_PROPERTY)
            connected = True
        finally:
            if not connected:
                await self._router.disconnect()
                if self._owns_session and self._session is not None:
                    await self._session.close()
                    self._session = None
                self._client = None
        self._log.info("[%s] Connected", self.host)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._router.disconnect()
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
        self._client = None
        self._log.info("[%s] Disconnected", self.host)

    def _on_event(self, prop: str, value: Any) -> None:
        """Updates `is_recording`, `last_known_storage`,
        `last_known_play`/`last_known_stop`, and `playback_interrupted` —
        never inferred from a request this session itself made, only from
        what the camera reports back (design principle 4).

        **Why `/transports/0/playback`'s own `speed` field is
        `playback_interrupted`'s trigger, not `/transports/0/stop`** (Phase
        8 item 2, part 2): `_put_playback()` arms and waits on
        `PLAYBACK_PROPERTY` for its own dual-check, so the exact WS delivery
        that satisfies a self-requested `pause()`/`shuttle()` is the same
        delivery this method receives — and `RestEventRouter.handle_event()`
        always calls `on_event()` *before* it wakes any `wait_for()` waiter
        (`events.py`), so `_playback_write_in_flight` is still `True` at the
        exact moment this method sees that event. `/transports/0/stop` is a
        second, independently-pushed property with no such ordering
        guarantee relative to `_put_playback()`'s own completion — using it
        as the trigger would leave a real race window between
        `_playback_write_in_flight` being cleared and `stop`'s own push
        arriving. `/transports/0/stop` is still subscribed and tracked
        (`last_known_stop`) as a corroborating signal, just not the
        authoritative one.

        **Trigger condition, broadened after the first two real-hardware
        runs** (`POCKET_6K_G2 v8.6`, 2026-08-05, both against a fixed
        `speed == 0` check — see `wait_for_playback_interrupt`'s docstring
        for what those two runs actually confirmed): compares the pushed
        `speed` against `_expected_speed` — the speed value this session's
        own last confirmed `enter_playback()`/`shuttle()`/`play()`/
        `pause()` call actually set — rather than checking `speed == 0`.
        Any deviation from what this session itself last requested counts
        as an interrupt, not only a full stop: a camera-initiated speed
        change that lands somewhere other than `0` (e.g. the camera
        dropping from `2.0` to `1.0` on its own) is exactly as much "not
        what I asked for" as landing on `0`, and the old fixed-`0` check
        had no way to catch it. Real-hardware-confirmed as a working
        trigger (`POCKET_6K_G2 v8.6`, 2026-08-05, two more runs after the
        two below) — but both those runs' interrupts also landed on `0`,
        so the "deviates to nonzero" branch specifically this broadening
        exists for is still unexercised on real hardware; see
        `wait_for_playback_interrupt`'s docstring for the full run trail.
        `_expected_speed` is set to `0.0` by
        `enter_playback()` (the camera opens paused — see `_put_playback`'s
        docstring) and updated by `_put_playback()` itself after each write
        it confirms carries a `speed` change.

        Symmetrically, `TRANSPORT_MODE_PROPERTY` reporting a mode other than
        `"Output"` while `_in_playback` is `True` and no
        `_set_transport_mode()` write is in flight
        (`_transport_mode_write_in_flight`) means the camera left playback
        mode without this session asking — the same "arrived while nothing
        of mine was in flight" test, applied to the other write path
        `enter_playback()`/`exit_playback()` use.

        **Both branches check *both* in-flight flags, not just their own
        write path's — real-hardware-confirmed defect this fixes
        (`POCKET_6K_G2 v8.6`, 2026-08-05,
        `tools/rest/verify_playback_interrupt.py`'s own sanity phase, the
        first real run of this feature).** The first version of this method
        gated the `PLAYBACK_PROPERTY` branch on `_playback_write_in_flight`
        alone. A self-requested `stop()` (`exit_playback()` ->
        `_set_transport_mode("InputPreview")`) sets only
        `_transport_mode_write_in_flight` — but leaving `"Output"` mode
        real-hardware-confirmed also pushes a `/transports/0/playback`
        event reporting `speed: 0` as a side effect (the camera stopping
        transport motion on its way out of playback), independent of
        anything `_put_playback()` did. That side-effect push arrived while
        `_playback_write_in_flight` was `False` (correctly — no
        `_put_playback()` call was active) and `_in_playback` was still
        `True`, so the original single-flag guard incorrectly set
        `playback_interrupted` on a call the sanity phase asserts should
        never trip it. The fix widens each branch to require *neither*
        flag in flight, since a write to either property can apparently
        cause a real push on the other — the two write paths are not as
        independent as the original per-property guard assumed.
        """
        if prop == RECORD_PROPERTY and isinstance(value, dict):
            recording = value.get("recording")
            if isinstance(recording, bool):
                self.is_recording = recording
                if recording:
                    self._recording_stopped.clear()
                else:
                    self._recording_stopped.set()
        elif prop == WORKINGSET_PROPERTY and isinstance(value, dict):
            # active_body=None: this is a pushed event, not a fresh
            # GET /media/active, so active_device resolution falls back to
            # each device's own activeDisk flag — real event data
            # (POCKET_6K_G2 v8.6, 2026-08-05) always carries it correctly.
            storage = _parse_storage_state(value, None)
            self.last_known_storage = storage
            self._check_low_storage(storage)
        elif prop == PLAYBACK_PROPERTY and isinstance(value, dict):
            speed = value.get("speed")
            if (
                isinstance(speed, (int, float))
                and self._in_playback
                and self._expected_speed is not None
                and speed != self._expected_speed
                and not self._playback_write_in_flight
                and not self._transport_mode_write_in_flight
            ):
                self.playback_interrupted.set()
        elif prop == TRANSPORT_MODE_PROPERTY and isinstance(value, dict):
            mode = value.get("mode")
            if (
                isinstance(mode, str)
                and mode != "Output"
                and self._in_playback
                and not self._transport_mode_write_in_flight
                and not self._playback_write_in_flight
            ):
                self.playback_interrupted.set()
                self._in_playback = False
        elif prop == PLAY_PROPERTY and isinstance(value, bool):
            self.last_known_play = value
        elif prop == STOP_PROPERTY and isinstance(value, bool):
            self.last_known_stop = value

    def _check_low_storage(self, storage: StorageState) -> None:
        if (
            self._low_storage_min_record_time_s is None
            and self._low_storage_min_space_bytes is None
        ):
            return
        if self._is_storage_low(storage):
            self._low_storage_event.set()

    def _is_storage_low(self, storage: StorageState) -> bool:
        device = storage.active_device
        if device is None:
            return True
        if (
            self._low_storage_min_record_time_s is not None
            and device.remaining_record_time <= self._low_storage_min_record_time_s
        ):
            return True
        return (
            self._low_storage_min_space_bytes is not None
            and device.remaining_space <= self._low_storage_min_space_bytes
        )

    async def wait_while_recording(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds, returning early if a stop is
        confirmed before then (e.g. a camera-initiated stop reported over
        the WS event feed). Mirrors `CameraSession.wait_while_recording`'s
        return-value contract exactly: `True` if still recording (or state
        unknown) when `timeout` elapses, `False` if a stop was confirmed
        before then. If `is_recording` is already `False` when called,
        returns `False` immediately — there is nothing left to hold.

        Real-hardware-confirmed defect this fixes (`POCKET_6K_G2`/
        `POCKET_6K_PRO v8.6`, 2026-08-03): the first version of this method
        had the *opposite* contract — `True` for "confirmed stopped",
        `False` for "still recording after timeout" — while
        `examples/rest_record_start_stop.py` used the same
        `if not held: stopped_early = True` pattern as the BLE example,
        which assumes `CameraSession`'s contract. Every cycle's
        `record_start` -> `wait_while_recording` -> `record_stop` sequence
        (which never sends an explicit stop before this call) misreported
        "recording stopped before the requested Ns" on every single cycle,
        even though the camera recorded the full requested duration each
        time — see docs/rest/session.md.

        Also clears the internal stop-event flag before waiting (mirroring
        `CameraSession.wait_while_recording`'s own clear-before-wait), so a
        stale flag left `.set()` by an *earlier* cycle's stop — possible
        when that cycle's `record_start()` confirmed via the secondary
        `GET` readback rather than the primary WS event, since only the
        event path clears it (see `_on_event`) — can never be mistaken for
        a fresh stop in this call.
        """
        if self.is_recording is False:
            return False
        self._recording_stopped.clear()
        try:
            await asyncio.wait_for(self._recording_stopped.wait(), timeout=timeout)
        except TimeoutError:
            return True
        return False

    async def wait_for_low_storage(
        self,
        *,
        min_record_time_s: float | None = None,
        min_space_bytes: int | None = None,
        timeout: float,
    ) -> bool:
        """Wait up to `timeout` seconds for the active storage device to
        drop at or below `min_record_time_s` seconds of remaining record
        time and/or `min_space_bytes` of remaining space (pass either or
        both; raises `ValueError` if neither is given). Driven entirely by
        `/media/workingset` `propertyValueChanged` events — never polled
        (`CLAUDE.md`: "never poll storage state in a loop") — so it only
        ever reacts to a value the camera actually pushed.

        **Contract, stated explicitly given this codebase's own history
        with an inverted-contract bug in `wait_while_recording` (see that
        method's docstring)**: returns `True` if low storage was observed
        — either already true when called, or a pushed update crossed the
        threshold before `timeout` elapsed. Returns `False` if `timeout`
        elapses with storage still healthy. This is the *opposite* polarity
        from `wait_while_recording`'s `True` (there, `True` means "nothing
        happened, still recording as expected") — deliberately, since
        `wait_for_low_storage`'s name reads naturally as "did low storage
        happen", not "did normal operation persist". Read the name, not the
        sibling method, when using this.

        If `last_known_storage` is `None` (no `/media/workingset` event has
        arrived yet, e.g. called immediately after connect), the already-low
        shortcut is skipped and this waits for the first qualifying push
        like any other case — there is nothing to evaluate yet.

        No active storage device at all (`active_device is None`, e.g. no
        card) counts as low — a caller reacting to low storage should also
        react to no storage, the same severity ordering
        `_require_storage_ready()` already uses for `record_start()`.

        Only one threshold can be armed at a time per session, mirroring
        `wait_while_recording`'s single `_recording_stopped` event — a
        second concurrent call would silently share (and clobber) the first
        call's threshold. Not intended for concurrent use from multiple
        tasks on the same session.
        """
        if min_record_time_s is None and min_space_bytes is None:
            raise ValueError(
                "wait_for_low_storage requires min_record_time_s and/or min_space_bytes"
            )
        self._low_storage_min_record_time_s = min_record_time_s
        self._low_storage_min_space_bytes = min_space_bytes
        try:
            if self.last_known_storage is not None and self._is_storage_low(
                self.last_known_storage
            ):
                return True
            self._low_storage_event.clear()
            try:
                await asyncio.wait_for(self._low_storage_event.wait(), timeout=timeout)
            except TimeoutError:
                return False
            return True
        finally:
            self._low_storage_min_record_time_s = None
            self._low_storage_min_space_bytes = None

    async def wait_for_playback_interrupt(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for a camera-initiated playback
        interrupt — a `/transports/0/playback` speed deviating from
        `_expected_speed` (the speed this session itself last set, so any
        camera-initiated change counts, not only a drop to `0`), or
        `/transports/0` reporting a mode other than `"Output"`, that
        arrived while this session had no matching write of its own in
        flight (see `_on_event`'s docstring). This is Phase 8 item 2's
        playback analogue of `wait_while_recording`'s camera-initiated-stop
        detection — pulling the card, or pressing stop/pause on the camera
        body, mid-playback.

        **Contract, same polarity as `wait_for_low_storage`, not
        `wait_while_recording`** (see that method's own docstring for why
        this codebase states this explicitly every time): returns `True` if
        an interrupt was observed — either already set when called, or a
        qualifying event arrived before `timeout` elapsed. Returns `False`
        if `timeout` elapses with nothing observed. Read the name, not a
        sibling method, when using this.

        Only meaningful while `_in_playback` is `True` — i.e. after
        `enter_playback()` has confirmed and before `exit_playback()`/
        `stop()` has. Calling this outside that window will simply time out,
        since `_on_event` only ever sets `playback_interrupted` while
        `_in_playback` is `True`.

        **Honest ceiling, same as every other camera-initiated-stop
        detection in this codebase**: this reports that playback stopped
        unexpectedly, never why. There is no error/fault event channel on
        this camera's REST API at all (`docs/rest/session.md`'s Phase 8
        item 3 write-up) — a caller wanting the reason still has to look at
        the camera itself.

        **Real-hardware-confirmed end to end, `POCKET_6K_G2 v8.6`,
        2026-08-05, five runs total.** The self-requested sanity half found
        and fixed a real false-positive this method's own trigger had (see
        `_on_event`'s docstring) — once fixed, every subsequent run
        confirmed a normal `pause()`/`play()`/`stop()` sequence never sets
        `playback_interrupted`. The positive case is confirmed too, three
        separate times: `play()`, then an out-of-band interrupt (card
        pulled or stop/pause pressed on the camera body), returned `True`
        after 13.5s, 19.1s, 6.2s, and 3.2s across four runs (the 13.5s and
        19.1s runs against the original fixed `speed == 0` check, the
        6.2s and 3.2s runs against the broadened `speed != _expected_speed`
        trigger that replaced it — both versions confirmed working).
        **Every one of these interrupts landed on a full stop** (`GET
        /transports/0` still reporting `mode: "Output"`, `last_known_stop`
        corroborating `True` where read) — the broadening's actual reason
        for existing, a camera-initiated speed change landing on some value
        other than `0`, has not itself been exercised on real hardware yet;
        pulling a card or pressing stop/pause on the camera body apparently
        always halts transport motion outright rather than leaving it at an
        intermediate speed. See `docs/rest/session.md`'s Phase 8 item 2
        section for the full five-run write-up.
        """
        if self.playback_interrupted.is_set():
            return True
        try:
            await asyncio.wait_for(self.playback_interrupted.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    @property
    def _rest_client(self) -> RestClient:
        if self._client is None:
            raise BMDConnectionError(
                f"[{self.host}] RestCameraSession is not connected — use 'async with'"
            )
        return self._client

    async def get_format(self) -> Format:
        body = await self._rest_client.get("/system/format")
        return _parse_format(body)

    async def supported_formats(self) -> tuple[SupportedFormat, ...]:
        spec = self.profile.rest_endpoint("/system/supportedFormats")
        if spec is None or not spec.supported:
            raise BMDUnsupportedError(
                f"[{self.host}] /system/supportedFormats is not confirmed supported in the "
                f"{self.profile.model_key} {self.profile.firmware} rest/ profile — run "
                "tools/rest/probe_endpoints.py against this camera first."
            )
        body = await self._rest_client.get("/system/supportedFormats")
        return tuple(_parse_supported_format(entry) for entry in body.get("supportedFormats", ()))

    async def storage_state(self) -> StorageState:
        workingset = await self._rest_client.get("/media/workingset")
        active = await self._rest_client.get("/media/active")
        return _parse_storage_state(workingset, active)

    async def device_info(self, device_name: str) -> DeviceInfo:
        """`GET /media/devices/{deviceName}` (Phase 10) — per the spec, the
        `state` a media device reports (`Mounted`, `Formatting`, ...).
        `device_name` is a `StorageDevice.device_name` value (e.g. `"sd0"`),
        not a mount name (`mount_names()`'s `"A001-sd1"` style) — the two
        are confirmed-different strings elsewhere in this codebase
        (`rest/media.py`'s module docstring), and this endpoint takes the
        device name specifically, per the spec's own parameter description
        ("as returned by deviceName member of Workingset or ActiveMedia").
        Used internally by `format_device()`'s completion poll; also a
        plain read verb on its own. Not yet real-hardware-confirmed."""
        body = await self._rest_client.get(f"/media/devices/{device_name}")
        return _parse_device_info(body)

    async def doformat_supported_filesystems(self) -> tuple[str, ...]:
        """`GET /media/devices/doformatSupportedFilesystems` (Phase 10) —
        the filesystem names `format_device()` will accept (spec example:
        `["ExFat", "HFS"]`). `format_device()` validates its own
        `filesystem` argument against a live call to this, the same
        live-capability-over-hardcoded-assumption discipline
        `set_camera_format()` already uses for codec/resolution/fps
        (design principle 7's REST sibling). Not yet real-hardware-
        confirmed."""
        body = await self._rest_client.get("/media/devices/doformatSupportedFilesystems")
        return tuple(fs for fs in body if isinstance(fs, str)) if isinstance(body, list) else ()

    async def clips(self) -> tuple[Clip, ...]:
        """`GET /clips/list`.

        Real-hardware-confirmed (`POCKET_6K_G2 v8.6`, 2026-08-03): with no
        SD card inserted, this endpoint returns `404 {"error": "No disk or
        media"}` rather than an empty `clipList` — a real, informative
        error, not evidence the endpoint is broken. Design principle 9's
        "reads are best-effort, return None" is for auxiliary metadata
        reads; "no storage media" is exactly what `BMDStorageError` exists
        to name (design principle 10), so it is re-raised as that rather
        than swallowed into a misleading empty tuple or left as a generic
        `BMDRestError` the caller has to decode by hand.
        """
        try:
            body = await self._rest_client.get("/clips/list")
        except BMDRestError as exc:
            if exc.status == 404:
                raise BMDStorageError(
                    f"[{self.host}] No storage media in {self.profile.model_key} — "
                    f"cannot list clips (GET /clips/list -> 404 {exc.body!r})"
                ) from exc
            raise
        return tuple(_parse_clip(entry) for entry in body.get("clipList", ()))

    async def timecode(self) -> Timecode:
        body = await self._rest_client.get("/transports/0/timecode")
        return decode_rest_timecode(body["timecode"])

    async def clip_timecode(self) -> Timecode:
        """Position within the current clip — `GET /transports/0/timecode`'s
        `clip` field, same BCD `HH:MM:SS:FF` encoding as `timecode()`'s
        `timecode` field, decoded with the same function. See
        `rest/timecode.py`'s module docstring for the spec citation and
        real-hardware confirmation. Like `timecode()`, this cannot show a
        dropped frame — it is a time-based position counter, confirmed
        empirically to advance smoothly through a real, operator-witnessed
        drop (`docs/rest/session.md`)."""
        body = await self._rest_client.get("/transports/0/timecode")
        return decode_rest_timecode(body["clip"])

    async def list_mount(self, path: str) -> tuple[dict[str, Any], ...]:
        """Raw directory listing at `path` — entries exactly as the camera
        reports them: `{"name": ..., "type": "file"|"directory", "mtime": ...}`,
        plus `"size"` for files (`docs/rest/transport.md`). `path` may be the
        bare `/mounts/` root (mount names) or a specific mount's own root
        (its direct children, e.g. a `Stills` entry with its own `mtime`) —
        both are outside `/control/api/v1`, so this always calls
        `api_prefixed=False`. Every subdirectory *one level below* a mount
        root 500s unconditionally (`docs/rest/transport.md`'s "The 500 is
        not Stills-specific") — never call this on anything deeper than a
        mount root."""
        body = await self._rest_client.get(path, api_prefixed=False)
        return (
            tuple(entry for entry in body if isinstance(entry, dict))
            if isinstance(body, list)
            else ()
        )

    async def mount_names(self) -> tuple[str, ...]:
        """`GET /mounts/`'s own real directory listing — the mount names
        actually available over HTTP (e.g. `("A001-sd1",)`), confirmed on
        real hardware to return `[{"name": ..., "type": "directory"}, ...]`
        (`docs/rest/transport.md`). Never derived from `deviceName`/`volume`
        by string transformation — see `rest/media.py`'s module docstring
        for why that mapping isn't trusted as a rule."""
        entries = await self.list_mount(MOUNTS_PATH)
        return tuple(
            entry["name"]
            for entry in entries
            if entry.get("type") == "directory" and isinstance(entry.get("name"), str)
        )

    async def path_exists(self, path: str) -> bool:
        """Whether `path` (e.g. a `/mounts/<name>/Stills/<file>` still)
        exists, without ever decoding its content — see `RestClient.exists()`
        for why a plain `GET` isn't safe for probing binary media files.
        `path` is a `/mounts/...` path, outside `API_BASE` — see
        `mount_names()`'s docstring. Purely opportunistic: used only by
        `rest/media.py`'s `guess_new_still_path()`, an opt-in, best-effort
        filename lookup — never by `wait_for_new_still()`'s actual
        confirmation, which relies on the Stills directory's own `mtime`
        instead (a directory listing 500s unconditionally, so a filename
        guess can be wrong; a real signal cannot)."""
        return await self._rest_client.exists(path, api_prefixed=False)

    # ── Writes (Phase 4) ─────────────────────────────────────────────────

    async def record_start(self) -> None:
        """Start recording, raising `BMDVerificationError` unless confirmed.

        Checks storage readiness first (design principle 10) — see
        `_require_storage_ready`.
        """
        await self._require_storage_ready()
        await self._set_recording_state(recording=True)

    async def record_stop(self) -> None:
        """Stop recording, raising `BMDVerificationError` unless confirmed.

        A no-op if `is_recording` already positively confirms the camera
        isn't recording — mirrors the BLE `CameraSession.record_stop`'s
        documented no-echo-on-redundant-write handling (docs/ble/recording.md).
        Whether this camera's REST record endpoint behaves the same way on a
        redundant `PUT` is unconfirmed, but the guard is harmless either
        way: `is_recording` is only ever notification-derived (design
        principle 4), never assumed, so skipping here never masks a real
        state mismatch.
        """
        if self.is_recording is False:
            return
        await self._set_recording_state(recording=False)

    async def _require_storage_ready(self) -> None:
        """Design principle 10's REST implementation for the record write
        path: read `storage_state()` before allowing a start, raising
        `BMDStorageError` for no active device or an active device with no
        remaining space — rather than letting the camera silently fail to
        save the clip.

        **Gates on `remaining_space`, not `remaining_record_time` (Phase 9
        fix).** The original version of this check gated on
        `remaining_record_time <= 0` instead. Real-hardware finding
        (`examples/rest_record_test_clip.py`, `POCKET_6K_G2 v8.6`,
        2026-08-05, all three runs): `remaining_record_time` is stale
        immediately after a `set_camera_format()` switch — it keeps
        reporting the *pre-switch* format's estimate (`50858s`) until a
        recording actually starts, at which point it snaps to the new
        format's real estimate (`15251s`, for the identical `remaining_space`
        the whole time). `remaining_space` itself stayed accurate throughout
        every run to date, including immediately after a switch. Gating a
        pre-flight check on the one field confirmed unreliable in exactly
        the window this check runs risked either a false pass (a large
        stale estimate masking a real shortage under the new format) or a
        false block (a small stale estimate blocking a start the new
        format could actually satisfy) — direction depends on which way the
        switch's bitrate changed, and no run to date happened to land in
        either failure mode, only the "stale, but still comfortably
        positive" case documented above. Rather than build format-switch-
        tracking machinery to detect staleness, this check now trusts the
        field that has never been observed stale in any run: real bytes
        free. `remaining_record_time` remains available via
        `storage_state()` for informational use; it just no longer gates
        this precondition.
        """
        storage = await self.storage_state()
        device = storage.active_device
        if device is None:
            raise BMDStorageError(
                f"[{self.host}] No active storage device in {self.profile.model_key} "
                f"{self.profile.firmware} — cannot start recording"
            )
        if device.remaining_space <= 0:
            raise BMDStorageError(
                f"[{self.host}] Active storage device '{device.device_name}' in "
                f"{self.profile.model_key} {self.profile.firmware} has no remaining "
                f"space ({device.remaining_space} bytes) — cannot start recording"
            )

    async def _set_recording_state(self, *, recording: bool) -> None:
        """`PUT /transports/0/record`, verified via the REST dual-check
        design principle 3 specifies: a WS `propertyValueChanged` event
        primary, a polled `GET` readback secondary. `204` on the `PUT` means
        accepted, not applied — neither check confirming raises
        `BMDVerificationError`.

        The capability check below only confirms this endpoint's `GET` side
        was swept — `tools/rest/probe_endpoints.py --probe-writes`
        deliberately never `PUT`s here (`NEVER_WRITE`: it would start or
        stop a real recording), so no profile will ever carry a confirmed
        `put_supported` for this path. The `PUT` itself is confirmed by this
        method's own verification below instead, on every call, the same
        way BLE's `record_start`/`record_stop` are verified without a
        profile-level "this command works" flag.

        `record_stop` gets a wider overall budget (`stop_verify_timeout_s`)
        than `record_start` — real-hardware finding, `POCKET_6K_G2 v8.6`,
        2026-08-05: an early run raised `BMDVerificationError` from
        `record_stop` on a recording that had actually succeeded (the next
        run's own "before" state showed the clip count had already
        incremented). `PUT /transports/0/record` itself is I/O-bound on stop
        — closing the `.braw` and writing its index — and measured at
        ~1.1-1.2s across three later runs, against `record_start`'s ~2ms.
        The primary WS wait still gets exactly `verify_timeout_s`, same as
        `record_start`; only the secondary readback's budget widens, and it
        is now polled every `RECORD_POLL_INTERVAL_S` for whatever's left of
        the overall budget instead of firing once — the same shape
        `select_clip()` already uses for its own timeline-membership poll.
        For `record_start`, the extra budget is zero, so this reduces to the
        original single-shot secondary check exactly as before.
        """
        endpoint = self.profile.rest_endpoint(RECORD_PROPERTY)
        if endpoint is None or not endpoint.supported:
            raise BMDUnsupportedError(
                f"[{self.host}] {RECORD_PROPERTY} is not confirmed present in the "
                f"{self.profile.model_key} {self.profile.firmware} rest/ profile — run "
                "tools/rest/probe_endpoints.py against this camera first."
            )

        action = "record_start" if recording else "record_stop"
        overall_timeout = self.verify_timeout_s if recording else self.stop_verify_timeout_s

        self._router.arm(RECORD_PROPERTY)
        await self._rest_client.put(RECORD_PROPERTY, {"recording": recording})
        event_value = await self._router.wait_for(RECORD_PROPERTY, timeout=self.verify_timeout_s)
        confirmed = _recording_flag(event_value)

        poll_deadline = time.monotonic() + max(0.0, overall_timeout - self.verify_timeout_s)
        while confirmed != recording:
            body = await self._rest_client.get(RECORD_PROPERTY)
            confirmed = _recording_flag(body)
            if confirmed == recording or time.monotonic() >= poll_deadline:
                break
            await asyncio.sleep(RECORD_POLL_INTERVAL_S)

        if confirmed != recording:
            raise BMDVerificationError(
                f"{action}: neither a WS '{RECORD_PROPERTY}' propertyValueChanged event "
                f"nor a GET readback confirmed recording={recording} within "
                f"{overall_timeout}s"
            )

    # ── Recording confirmation (Phase 9) ────────────────────────────────

    async def confirm_new_clip(
        self, clips_before: tuple[Clip, ...], storage_before: StorageState | None = None
    ) -> RecordingResult:
        """Identify the clip a `record_start()`/`record_stop()` cycle just
        wrote, by diffing `clips_before` (a snapshot the caller took, e.g.
        via `clips()`, before calling `record_start()`) against a fresh
        `clips()` read taken now. Formalizes the diff
        `examples/rest_record_test_clip.py` did by hand across three real
        real-hardware runs (`POCKET_6K_G2 v8.6`, 2026-08-05) into a reusable
        method.

        **Why `clips_before` must be caller-supplied, not automatic.**
        Unlike `is_recording`/`last_known_storage`/everything else on
        `CameraState`, `GET /clips/list` has no WS event of any kind — this
        cannot be notification-driven (design principle 4 has nothing to
        observe here), and there is no "just written" flag on a `Clip`
        either (design principle 9: reads are best-effort, not proof of
        anything not directly reported). A before-snapshot the caller
        already took is the only way to name which clip is new.

        **Precondition, stated loudly rather than left implicit: same
        connected session only.** `clip_unique_id` is real-hardware-
        confirmed *not* stable across reconnects (`docs/rest/session.md`'s
        `clips()` section: the same two physical files reported
        `clipUniqueId` `15`/`16` in one session and `1`/`2` in a later one,
        minutes apart, no reformat or recording in between). Passing a
        `clips_before` snapshot captured in a *different* session than the
        one this method runs in can produce a silently wrong "new clip" or
        a false ambiguity — this only means what it claims to mean when
        `clips_before` was captured earlier in the same `async with
        RestCameraSession(...)` block.

        Raises `BMDVerificationError` if zero new clips are found (the
        recording never happened, or hasn't been indexed yet), and also if
        **more than one** new clip appears — this method does not guess
        which one is "the" recording, matching this codebase's established
        refusal to guess under ambiguity (`_resolve_supported_format`'s
        `sensor_resolution` ambiguity is the closest precedent).
        `BMDVerificationError` rather than `BMDUnsupportedError` for both
        cases: this is a verification question (can this session confirm
        what it wrote), not a capability question (does the camera support
        something). **Honestly unexercised**: no real-hardware run in this
        codebase's history has ever produced more than one new clip from a
        single `record_start`/`record_stop` cycle — this branch is
        defensive, not confirmed-necessary.

        If `storage_before` is given and both it and a fresh
        `storage_state()` report an `active_device`, `bytes_written` is
        `storage_before.active_device.remaining_space` minus the fresh
        reading's — the same computation
        `examples/rest_record_test_clip.py` did by hand, since `Clip` has
        no size field of its own (the same gap Phase 6's `rest/media.py`
        hit for stills). `None` if `storage_before` is omitted or either
        snapshot has no active device.
        """
        clips_after = await self.clips()
        ids_before = {clip.clip_unique_id for clip in clips_before}
        new_clips = [clip for clip in clips_after if clip.clip_unique_id not in ids_before]

        if not new_clips:
            raise BMDVerificationError(
                f"[{self.host}] confirm_new_clip(): no new clip found in GET /clips/list "
                f"— {len(clips_before)} clips before, {len(clips_after)} now"
            )
        if len(new_clips) > 1:
            new_ids = [clip.clip_unique_id for clip in new_clips]
            raise BMDVerificationError(
                f"[{self.host}] confirm_new_clip(): {len(new_clips)} new clips found "
                f"({new_ids}) — cannot tell which one is the recording just confirmed"
            )
        clip = new_clips[0]

        bytes_written: int | None = None
        if storage_before is not None and storage_before.active_device is not None:
            storage_after = await self.storage_state()
            if storage_after.active_device is not None:
                bytes_written = (
                    storage_before.active_device.remaining_space
                    - storage_after.active_device.remaining_space
                )

        return RecordingResult(clip=clip, bytes_written=bytes_written)

    # ── Writes (Phase 5) ─────────────────────────────────────────────────

    async def set_camera_format(
        self,
        codec: str,
        variant: str,
        resolution: str,
        fps: str,
        *,
        sensor_resolution: tuple[int, int] | None = None,
    ) -> None:
        """Set codec family, quality variant, resolution, and frame rate
        together via one `PUT /system/format` — the REST analogue of the BLE
        `CameraSession.set_camera_format` orchestration, but collapsed to a
        single request instead of three separate packets, since REST's
        `/system/format` carries codec, resolution, and frame rate together.

        Takes the same profile vocabulary a BLE script already uses
        (`"BRAW"`/`"5:1"`, `"4K DCI"`, `"23.98"`) so a script's vocabulary is
        identical on either transport — `codec`/`variant` are translated to
        REST's own spelling via `rest/mapping.py`'s `resolve_rest_codec_name`
        (preferring the profile's confirmed `rest/<fw>.json` `format_names`
        entry, falling back to the derivation rule only when unconfirmed);
        `resolution` and `fps` need no translation at all — REST already
        uses this repo's own `resolutions` width/height and `fps_modes`
        names/strings directly (see `mapping.py`'s module docstring).

        `codec`/`variant`/`resolution`/`fps` are validated against the
        profile's own tables first (`require_codec`/`require_resolution`/
        `require_fps_mode`, shared with BLE — design principle 1), raising
        `ValueError` for a name the profile doesn't know at all. None of
        BLE's `dimension_enums`, `m_rate`, `frame_flags`, `known_unreachable`,
        or `max_fps_int` are consulted here — those stay BLE-only (do not
        delete them; `CameraSession` still needs them). Instead, the
        requested combination is resolved against the camera's own **live**
        `supported_formats()` capability matrix (design principle 7's REST
        sibling to BLE's static ceiling checks — see
        `_resolve_supported_format`), raising `BMDUnsupportedError` when the
        camera doesn't report offering it, before any write is attempted.

        **`sensorResolution` is derived from that same matched entry, not
        preserved from the current format.** Real-hardware-confirmed defect
        this fixes (`POCKET_6K_G2 v8.6`, 2026-08-03): `GET
        /system/supportedFormats` pairs `4096×2160` `recordResolution` with
        **different** `sensorResolution` values depending on codec — `ProRes`
        pairs it with `5744×3024`, `BRaw` with `4096×2160` (see
        docs/rest/transport.md). The first version of this method preserved
        whatever `sensorResolution` the *current* format happened to have,
        which is only ever correct when a write doesn't cross that boundary.
        A confirmed `ProRes/422/4K DCI` write followed immediately by
        `BRAW/5:1/4K DCI` sent `BRaw`'s codec/resolution/fps fields together
        with `ProRes`'s stale `5744×3024` sensorResolution — an internally
        inconsistent combination the camera correctly rejected with
        `400 {"error": "Format is not supported"}`, not a 501/`BMDUnsupportedError`,
        since nothing about the *requested* combination was actually
        unsupported. `sensorResolution` is now always set to
        `_resolve_supported_format`'s matched entry — the exact pairing the
        camera itself declared for this `(codec, recordResolution, fps)` —
        rather than left untouched. If more than one `supported_formats()`
        entry matches with *different* `sensorResolution` values (e.g.
        `ProRes` at `1920×1080`, which pairs with three different sensor
        resolutions — see docs/rest/transport.md), `_resolve_supported_format`
        raises `BMDUnsupportedError` naming the ambiguity rather than guessing
        which one the caller wants — *unless* `sensor_resolution` (below)
        disambiguates it explicitly. Every other field this method doesn't
        touch (`offSpeedEnabled`/`offSpeedFrameRate`/min/max off-speed) is still
        preserved from a `GET /system/format` taken right before the write —
        never a hand-built partial body, since a partial one risks resetting
        those to defaults.

        `sensor_resolution`, if given, must be one of `supported_formats()`'s
        own `sensor_resolution` values already paired with the requested
        `(codec, recordResolution, fps)` — it disambiguates among the
        camera's own offered pairings, it does not let a caller ask for an
        arbitrary one. `BMDUnsupportedError` if the camera doesn't pair that
        exact `sensor_resolution` with the requested combination.
        `tools/rest/sweep_camera_format.py` is this parameter's first real
        caller — sweeping every pairing systematically, including the
        ambiguous ones this method would otherwise refuse, is exactly what
        motivated adding it rather than leaving it as unreachable future
        work.

        Verified via the same dual-check as `record_start`/`record_stop`:
        a WS `propertyValueChanged` event on `/system/format` primary, a
        `GET /system/format` readback secondary — `BMDVerificationError` if
        neither confirms the requested
        `codec`/`frameRate`/`recordResolution`/`sensorResolution` within
        `verify_timeout_s`.
        """
        self.profile.require_codec(codec, variant)
        resolution_spec = self.profile.require_resolution(resolution)
        self.profile.require_fps_mode(fps)

        endpoint = self.profile.rest_endpoint(FORMAT_PROPERTY)
        if endpoint is None or not endpoint.put_supported:
            raise BMDUnsupportedError(
                f"[{self.host}] PUT {FORMAT_PROPERTY} is not confirmed supported in the "
                f"{self.profile.model_key} {self.profile.firmware} rest/ profile — run "
                "tools/rest/probe_endpoints.py --probe-writes against this camera first."
            )

        rest_codec = resolve_rest_codec_name(self.profile.rest.format_names, codec, variant)
        record_resolution = (resolution_spec.width, resolution_spec.height)
        matched = await self._resolve_supported_format(
            rest_codec, fps, record_resolution, sensor_resolution=sensor_resolution
        )

        current = await self._rest_client.get(FORMAT_PROPERTY)
        body = dict(current)
        body["codec"] = rest_codec
        body["frameRate"] = fps
        body["recordResolution"] = {
            "width": resolution_spec.width,
            "height": resolution_spec.height,
        }
        body["sensorResolution"] = {
            "width": matched.sensor_resolution[0],
            "height": matched.sensor_resolution[1],
        }

        self._log.info(
            "[%s] Setting camera format -> codec=%s variant=%s resolution=%s fps=%s (PUT %s)",
            self.host,
            codec,
            variant,
            resolution,
            fps,
            FORMAT_PROPERTY,
        )
        self._router.arm(FORMAT_PROPERTY)
        await self._rest_client.put(FORMAT_PROPERTY, body)
        event_value = await self._router.wait_for(FORMAT_PROPERTY, timeout=self.verify_timeout_s)
        confirmed = _format_matches(
            event_value,
            codec=rest_codec,
            frame_rate=fps,
            resolution=record_resolution,
            sensor_resolution=matched.sensor_resolution,
        )
        if not confirmed:
            readback = await self._rest_client.get(FORMAT_PROPERTY)
            confirmed = _format_matches(
                readback,
                codec=rest_codec,
                frame_rate=fps,
                resolution=record_resolution,
                sensor_resolution=matched.sensor_resolution,
            )
        if not confirmed:
            raise BMDVerificationError(
                f"set_camera_format({codec} {variant} {resolution} {fps}): neither a WS "
                f"'{FORMAT_PROPERTY}' propertyValueChanged event nor a GET readback "
                f"confirmed codec={rest_codec!r} frameRate={fps!r} "
                f"recordResolution={record_resolution} "
                f"sensorResolution={matched.sensor_resolution} within {self.verify_timeout_s}s"
            )

    async def _resolve_supported_format(
        self,
        rest_codec: str,
        fps: str,
        record_resolution: tuple[int, int],
        *,
        sensor_resolution: tuple[int, int] | None = None,
    ) -> SupportedFormat:
        """Design principle 7's REST sibling to BLE's static
        `known_unreachable`/`max_fps_int` checks: instead of a hand-
        maintained profile ceiling, ask the camera's own live
        `GET /system/supportedFormats` capability matrix (`supported_formats()`)
        which entry offers `rest_codec` at `record_resolution` and `fps`
        together, raising `BMDUnsupportedError` immediately when none does —
        before any write is attempted.

        Returns the matched entry (not just a bool) so its `sensor_resolution`
        can be carried into the write body — see `set_camera_format`'s
        docstring for the real-hardware defect this closes. When `sensor_resolution`
        is given, only entries whose own `sensor_resolution` equals it are
        considered — `set_camera_format`'s explicit disambiguation path.
        Otherwise, more than one entry matching with *different*
        `sensor_resolution` values raises `BMDUnsupportedError`: this method
        has no evidence for which one the caller wants, and guessing risks
        reproducing the exact "internally inconsistent body" failure this
        exists to prevent.
        """
        formats = await self.supported_formats()
        matches = [
            entry
            for entry in formats
            if entry.record_resolution == record_resolution
            and rest_codec in entry.codecs
            and fps in entry.frame_rates
        ]
        if sensor_resolution is not None:
            matches = [entry for entry in matches if entry.sensor_resolution == sensor_resolution]
        if not matches:
            detail = (
                f"sensorResolution={sensor_resolution} " if sensor_resolution is not None else ""
            )
            raise BMDUnsupportedError(
                f"[{self.host}] {self.profile.model_key} {self.profile.firmware} does not "
                f"report offering codec={rest_codec!r} at recordResolution={record_resolution} "
                f"{detail}frameRate={fps!r} in GET /system/supportedFormats"
            )
        sensor_resolutions = {entry.sensor_resolution for entry in matches}
        if len(sensor_resolutions) > 1:
            raise BMDUnsupportedError(
                f"[{self.host}] {self.profile.model_key} {self.profile.firmware} reports "
                f"{len(matches)} GET /system/supportedFormats entries for codec={rest_codec!r} "
                f"recordResolution={record_resolution} frameRate={fps!r} with different "
                f"sensorResolution values ({sorted(sensor_resolutions)}) — set_camera_format "
                "has no way to choose between them yet (pass sensor_resolution to disambiguate)"
            )
        return matches[0]

    # ── Reads (Phase 7 — playback and gallery) ────────────────────────────

    async def timeline_clip_ids(self) -> tuple[int, ...]:
        """`GET /timelines/0`, returning the clip ids the camera currently
        reports in its playback timeline — see `select_clip()`'s docstring
        for what "timeline" means on this camera: always every clip
        sharing the camera's *current* format, never a caller-curated
        subset. Read-only — does not call `select_clip()`, does not switch
        format, does not sync anything; it only reports whatever the
        camera already has active. Parsed via the same
        `_parse_timeline_clip_ids()` `select_clip()`'s own poll uses (real
        shape confirmed `POCKET_6K_PRO v8.6`, 2026-08-04: `{"clips":
        [{"clipUniqueId": int, "frameCount": int}]}`).

        Added for `examples/check_timeline_stale_entries.py`, which used it
        to answer `select_clip()`'s finding #1 open question — whether
        skipping `DELETE /timelines/0` (`501` on this firmware) ever
        leaves cross-format entries behind after switching to a
        differently-formatted clip. **Answer, `POCKET_6K_G2 v8.6`,
        2026-08-04, confirmed symmetrically in both directions (clip A ->
        clip B and clip B -> clip A, two different codec/resolution
        combinations): no.** Every readback contained exactly the
        newly-selected clip's own format group, nothing left over from
        the previous one. `POST /timelines/0/add` fully replaces the
        timeline's contents on this firmware even without the `DELETE`
        that structurally cannot run — see `select_clip()`'s own
        docstring for the closed-out finding.
        """
        endpoint = self.profile.rest_endpoint(TIMELINE_PATH)
        if endpoint is None or not endpoint.supported:
            raise BMDUnsupportedError(
                f"[{self.host}] {TIMELINE_PATH} is not confirmed present in the "
                f"{self.profile.model_key} {self.profile.firmware} rest/ profile — run "
                "tools/rest/probe_endpoints.py against this camera first."
            )
        body = await self._rest_client.get(TIMELINE_PATH)
        return tuple(_parse_timeline_clip_ids(body))

    # ── Writes (Phase 7 — playback and gallery) ───────────────────────────

    async def select_clip(self, clip_unique_id: int, *, poll_interval_s: float = 0.5) -> None:
        """Make `clip_unique_id` (`Clip.clip_unique_id`, from `clips()`,
        Phase 3) playable: switch the camera's format to match that clip's
        own recorded format if it doesn't already (`set_camera_format`,
        Phase 5), then `POST /timelines/0/add` to sync the camera's
        playback timeline. Required before `enter_playback()`/`play()` can
        show anything.

        **Replaces the original `set_timeline(clip_unique_ids: list[int])`
        design — real hardware disproved that design's core premise.** The
        plan this was built from assumed a caller could hand-curate an
        arbitrary ordered subset of clips into one custom playlist. Four
        rounds of real-hardware testing (`POCKET_6K_G2`/`POCKET_6K_PRO
        v8.6`, 2026-08-04, `docs/rest/session.md` carries the full trail)
        showed that isn't how this camera works at all:

        1. `DELETE /timelines/0` returns `501` on this firmware — caught
           and logged, not propagated, so a `POST` is still attempted.
        2. `POST /timelines/0/add`'s only accepted body shape is
           `{"clips": [{"clipUniqueId": id}, ...]}` — four alternate
           shapes tried in a Postman debugging session all failed
           (`{"clips": [id]}` and `{"clipUniqueIds": [id]}` both
           `"Not implemented for this device"`, `{"clip": {...}}` a `400
           "Invalid clips data"`, `PUT /timelines/0` a flat `405 Method
           Not Allowed`).
        3. **The decisive finding:** with the camera's format confirmed
           `ProRes:Proxy` at `4096x2160p24` immediately beforehand,
           `POST`ing `{"clips": [{"clipUniqueId": 1}]}` produced a
           `GET /timelines/0` readback of **seven** clips
           (`[10, 1, 9, 8, 7, 5, 6]`) — every clip on the card whose own
           recorded format was `ProRes:Proxy @ 4096x2160p24`, not just
           clip `1`. Repeating the exact same request with
           `clipUniqueId: 5` instead of `1` (format re-verified unchanged,
           no camera-body interaction in between) produced the *identical*
           seven-clip set. The requested id does not select which clips
           end up in the timeline. Independently, the camera's own
           on-screen playback view (photographed live) showed `"CLIP 1/7"`
           for the same seven-clip group — confirming this is native
           camera behavior, not a REST-API-specific quirk: **the
           "timeline" is always every clip matching the camera's current
           active format, full stop.**

        So `select_clip()` doesn't build a playlist — nothing on this
        camera can. It picks one clip, ensures the camera's format matches
        it (which determines the *entire* resulting playable group, same
        as every other clip sharing that format), and confirms that
        `clip_unique_id` specifically appears somewhere in the resulting
        `GET /timelines/0` readback — membership, not the exact-list
        equality `set_timeline()` used to check, since the real result is
        never just the one clip requested.

        **Real-hardware-confirmed, `POCKET_6K_G2` and `POCKET_6K_PRO v8.6`,
        2026-08-04:** this exact combination — the format check, the
        `set_camera_format()` switch when needed, and the `DELETE`-then-
        `POST` timeline sync — ran clean end to end on both cameras via
        `examples/rest_playback.py`, each step's own dual-check passing
        (this method's readback poll included). The clip requested here
        (`clip_unique_id=1`) already matched the camera's format on both
        runs, so this specific run didn't exercise the `set_camera_format`
        branch — that piece's own evidence is still Phase 5's, not new
        from this run. **Closed, `POCKET_6K_G2 v8.6`, 2026-08-05**: a later
        run (`examples/rest_playback.py`'s
        `_select_clip_trying_all_sensor_resolutions()`, added for the third
        gap below) selected a *genuinely* mismatched `clip_unique_id=1`
        (`ProRes:HQ @ 1920x1080p25` against a `BRaw:3_1` camera) and this
        method's own `set_camera_format()` switch confirmed cleanly,
        immediately followed by a successful `POST`/poll — this method's
        switch branch now has direct real-hardware evidence of its own, not
        just Phase 5's by extension. Finding #1's open question — whether
        skipping the
        `DELETE` clear leaves stale cross-format entries in the timeline —
        is since closed: `examples/check_timeline_stale_entries.py`
        confirmed, symmetrically in both directions, that `POST
        /timelines/0/add` fully replaces the timeline's contents even
        without `DELETE` ever running (see `timeline_clip_ids()`'s
        docstring for the full readback) — **though `tools/rest/diagnose_timeline.py`**
        (2026-08-05, `docs/rest/session.md`'s finding #7) since found `POST`
        is a no-op when the target clip's format doesn't already match the
        camera's live one, which means this "fully replaces" reading, like
        every other confirmed success here, couldn't at the time distinguish
        the `POST` doing that replacement from the *format switch* that
        preceded it (both stale-entries runs switched format first) already
        having done so on its own — **closed, `POCKET_6K_G2 v8.6`,
        2026-08-05, `tools/rest/diagnose_timeline.py --skip-post`**: switched
        format to match a clip with zero `DELETE`/`POST` ever sent, and `GET
        /timelines/0` immediately after already reported that clip — the
        format switch alone populates the timeline; `POST /timelines/0/add`
        has never been shown to do anything on this firmware, in any run to
        date. `select_clip()` still sends it (harmless, and this run only
        tested the single-matching-clip case, not finding #4's
        seven-clips-share-a-format scenario), but it is confirmed
        dead weight, not confirmed necessary. `resolve_ble_codec_name`
        (`mapping.py`) can raise
        `BMDUnsupportedError` if a clip's REST codec string isn't in the
        profile's confirmed `format_names` table (no derivation fallback
        — see `mapping.py`'s own docstring for
        why guessing backwards isn't safe); `_resolution_name_for_dimensions`
        can do the same if a clip's pixel dimensions don't match any
        profile `resolutions` entry. Both are real gaps this method
        surfaces loudly rather than papering over, not proof against them.

        **A third, confirmed-real gap inherited from `set_camera_format()`
        itself, `POCKET_6K_G2 v8.6`, 2026-08-04:** some `(codec,
        recordResolution, fps)` combinations pair with more than one
        `sensorResolution` in `supported_formats()` (real case: `ProRes` at
        `1920x1080` pairs with three). `set_camera_format()` refuses to
        guess and raises `BMDUnsupportedError` unless its own
        `sensor_resolution` parameter disambiguates — but this method has
        no `sensor_resolution` parameter of its own to pass one through,
        and `Clip` (`clips()`, Phase 3) carries no `sensorResolution` field
        to disambiguate with even if it did. A clip recorded at one of
        these ambiguous resolutions cannot be selected via this method
        unless the camera already happens to be at a matching format —
        deliberately: this method still raises immediately rather than
        guessing (design principle 7). `examples/rest_playback.py` composes
        a retry around this exact boundary instead: catches the
        `BMDUnsupportedError`, re-derives the real candidate
        `sensorResolution` values from `supported_formats()`, and tries
        `set_camera_format()` with each — since this method's own format
        comparison above never checks `sensorResolution`, a second call
        after a candidate is set sees the format as already matching. See
        `docs/rest/session.md`'s "What's deliberately out of scope" for the
        full write-up. **Real-hardware-confirmed, `POCKET_6K_G2 v8.6`,
        2026-08-05**: the exact `ProRes:HQ @ 1920x1080p25` combination
        above, retried against real hardware — the ambiguity error fired,
        the retry found the same 3 candidates, and the first one tried
        succeeded on the first attempt.

        Verified by polling `GET /timelines/0` (default every 0.5s,
        `poll_interval_s`) until `_parse_timeline_clip_ids()`'s reading
        contains `clip_unique_id`, or `verify_timeout_s` elapses
        (`BMDVerificationError`) — no WS event shape is known for this
        resource, so unlike every other write in this session, there is no
        primary/secondary dual-check here, only a readback poll (the same
        shape `rest/media.py`'s `wait_for_new_still()` uses for a resource
        with no known event channel either).
        """
        matches = [clip for clip in await self.clips() if clip.clip_unique_id == clip_unique_id]
        if not matches:
            raise ValueError(
                f"[{self.host}] No clip with clip_unique_id={clip_unique_id} in "
                f"GET /clips/list on {self.profile.model_key} {self.profile.firmware}"
            )
        clip = matches[0]
        if clip.codec is None or clip.video_format is None:
            raise BMDUnsupportedError(
                f"[{self.host}] clip_unique_id={clip_unique_id} ({clip.file_path}) has no "
                "codec/videoFormat in GET /clips/list — cannot determine its format"
            )
        parsed = _parse_video_format(clip.video_format)
        if parsed is None:
            raise BMDUnsupportedError(
                f"[{self.host}] clip_unique_id={clip_unique_id}'s videoFormat "
                f"{clip.video_format!r} doesn't match the confirmed "
                "'<width>x<height>p<fps>' shape — cannot resolve its format"
            )
        width, height, fps_str = parsed

        current = await self.get_format()
        format_matches = (
            current.codec == clip.codec
            and current.record_resolution == (width, height)
            and current.frame_rate == fps_str
        )
        if not format_matches:
            self._log.info(
                "[%s] clip_unique_id=%s format (%s @ %sx%sp%s) does not match the camera's "
                "current format (%s @ %sx%sp%s) — switching before syncing the timeline",
                self.host,
                clip_unique_id,
                clip.codec,
                width,
                height,
                fps_str,
                current.codec,
                current.record_resolution[0],
                current.record_resolution[1],
                current.frame_rate,
            )
            ble_pair = resolve_ble_codec_name(self.profile.rest.format_names, clip.codec)
            if ble_pair is None:
                raise BMDUnsupportedError(
                    f"[{self.host}] clip_unique_id={clip_unique_id}'s codec {clip.codec!r} is "
                    f"not in the {self.profile.model_key} {self.profile.firmware} rest/ "
                    "profile's format_names table — cannot resolve it to a (family, variant) "
                    "set_camera_format() accepts; populate format_names from a sweep first."
                )
            family, variant = ble_pair
            resolution_name = _resolution_name_for_dimensions(
                self.profile.resolutions, width, height
            )
            if resolution_name is None:
                raise BMDUnsupportedError(
                    f"[{self.host}] clip_unique_id={clip_unique_id}'s recordResolution "
                    f"{(width, height)} has no matching entry in the "
                    f"{self.profile.model_key} {self.profile.firmware} profile's resolutions "
                    "table — cannot resolve it to a profile resolution name"
                )
            await self.set_camera_format(family, variant, resolution_name, fps_str)

        endpoint = self.profile.rest_endpoint(TIMELINE_PATH)
        if endpoint is None or not endpoint.supported:
            raise BMDUnsupportedError(
                f"[{self.host}] {TIMELINE_PATH} is not confirmed present in the "
                f"{self.profile.model_key} {self.profile.firmware} rest/ profile — run "
                "tools/rest/probe_endpoints.py against this camera first."
            )

        try:
            await self._rest_client.delete(TIMELINE_PATH)
        except BMDUnsupportedError:
            self._log.warning(
                "[%s] DELETE %s is not implemented (501) on this firmware — proceeding "
                "to POST %s without clearing the existing timeline first",
                self.host,
                TIMELINE_PATH,
                TIMELINE_ADD_PATH,
            )
        await self._rest_client.post(
            TIMELINE_ADD_PATH, {"clips": [{"clipUniqueId": clip_unique_id}]}
        )

        deadline = time.monotonic() + self.verify_timeout_s
        current_ids: list[int] = []
        while True:
            body = await self._rest_client.get(TIMELINE_PATH)
            current_ids = _parse_timeline_clip_ids(body)
            if clip_unique_id in current_ids:
                return
            if time.monotonic() >= deadline:
                raise BMDVerificationError(
                    f"select_clip({clip_unique_id}): GET {TIMELINE_PATH} never reported "
                    f"clip_unique_id={clip_unique_id} within {self.verify_timeout_s}s "
                    f"(last read: {current_ids})"
                )
            await asyncio.sleep(poll_interval_s)

    async def enter_playback(self) -> None:
        """Switch the camera into playback mode — `PUT /transports/0
        {"mode": "Output"}`. `select_clip()` should be called first; there
        is nothing to show otherwise. Real-hardware-confirmed as part of
        the full Phase 7 sequence (`POCKET_6K_G2` and `POCKET_6K_PRO v8.6`,
        2026-08-04, `examples/rest_playback.py`).

        **Format precondition, confirmed real on the camera body
        (`POCKET_6K_PRO v8.6`, 2026-08-04):** a clip only plays if the
        camera's *current* codec/quality/resolution/fps matches the format
        the clip was recorded with — in fact real-hardware testing showed
        this isn't just a playback-time restriction but the very thing that
        defines the camera's entire playable timeline: every clip sharing
        the current format, and nothing else (see `select_clip()`'s
        docstring for the full evidentiary trail). `select_clip()` already
        switches format for you before syncing the timeline, so a caller
        going through it doesn't need to think about this. Calling
        `enter_playback()`/`play()`/`shuttle()` without having gone through
        `select_clip()` first — or with the camera's format having changed
        since — is expected to dual-check-fail with a `BMDVerificationError`
        (nothing observably changes) rather than a clearer diagnosis naming
        the mismatch.

        Sets `_in_playback = True` and clears `playback_interrupted` on
        success (Phase 8 item 2, part 2) — explicitly here, not left to
        `_on_event`, since a call confirmed only via the secondary `GET`
        readback never generates a WS event for `_on_event` to react to.
        Clearing here means a stale `playback_interrupted` left `.set()` by
        an *earlier* playback cycle's interrupt can never be mistaken for a
        fresh one in this cycle. Also resets `_expected_speed` to `0.0` —
        the camera opens playback paused (`_put_playback`'s docstring), so
        that's the correct baseline `_on_event`'s speed-deviation check
        compares against until a `play()`/`shuttle()` call updates it.
        """
        await self._set_transport_mode("Output")
        self._in_playback = True
        self.playback_interrupted.clear()
        self._expected_speed = 0.0

    async def exit_playback(self) -> None:
        """Leave playback mode, back to live view — `PUT /transports/0
        {"mode": "InputPreview"}`. Real-hardware-confirmed as part of the
        full Phase 7 sequence (`POCKET_6K_G2` and `POCKET_6K_PRO v8.6`,
        2026-08-04, `examples/rest_playback.py`).

        **Confirmed: this method reverts the camera's format to whatever
        it was before playback mode was entered — `POCKET_6K_G2 v8.6`,
        2026-08-04, isolated across three real-hardware runs the same
        day.** A `select_clip()` call that switched the camera's format
        (e.g. `ProRes:HQ @ 4096x2160p25` -> the requested clip's own
        `BRaw:5_1 @ 6144x3456p25`) was followed by three different
        truncated sequences, each checking `GET /system/format`
        immediately after the last call made:

        1. `select_clip()` alone (no `enter_playback()` or beyond) — still
           `BRaw:5_1 @ 6144x3456p25`, no revert.
        2. `select_clip()` + `enter_playback()` alone (no `exit_playback()`
           at all) — still `BRaw:5_1 @ 6144x3456p25`, no revert.
        3. `select_clip()` + `enter_playback()` + `exit_playback()`
           (`play()`/`pause()`/`seek()`/`shuttle()`/`stop()` all skipped
           in between) — back to `ProRes:HQ @ 4096x2160p25`, the exact
           pre-`select_clip()` format.

        Runs 1 and 2 rule out `select_clip()`/`set_camera_format()` and
        `enter_playback()` as the cause; only removing *this* method from
        the sequence removes the revert. **`exit_playback()`'s
        `PUT /transports/0 {"mode": "InputPreview"}` — leaving playback
        mode — is what triggers the camera to restore whatever format
        preceded entry into `Output` mode.** One caveat worth naming: in
        every test so far, `select_clip()` was the only thing that ever
        changed format before `enter_playback()` ran, so "reverts to the
        pre-`select_clip()` format" and "reverts to whatever format was
        active immediately before `Output` mode" are indistinguishable
        from this evidence alone — they happen to be the same value in
        every run. This method does not compensate for it or expose any
        way to opt out — a caller that needs a specific format after
        leaving playback must call `set_camera_format()` again explicitly
        after this method returns, not assume the camera holds
        `select_clip()`'s switch. See docs/rest/session.md's
        `enter_playback()` / `exit_playback()` section for the full
        three-run trail.

        Sets `_in_playback = False` on success (Phase 8 item 2, part 2) —
        explicitly here for the same reason `enter_playback()` sets it
        `True` explicitly: a call confirmed only via the secondary `GET`
        readback never generates a WS event for `_on_event`'s own
        mode-left-`"Output"` branch to react to.
        """
        await self._set_transport_mode("InputPreview")
        self._in_playback = False

    async def _set_transport_mode(self, mode: str) -> None:
        """`PUT /transports/0 {"mode": mode}`, dual-check verified exactly
        like `_set_recording_state`/`set_camera_format`: a WS
        `propertyValueChanged` event on `TRANSPORT_MODE_PROPERTY` primary,
        a `GET` readback secondary.

        Only `"InputPreview"` and `"Output"` are valid here — `"InputRecord"`
        is read-only on this endpoint and is set through
        `/transports/0/record` instead
        (`tools/rest/probe_endpoints.py`'s write catalog skips this
        endpoint's same-value probe whenever the camera currently reports
        `InputRecord`, since its own `PUT` cannot accept that value back —
        `docs/rest/transport.md`'s reshaping table). This method does not
        enforce that itself; the camera's own rejection is the real guard,
        surfacing here as a failed verification.

        Holds `_transport_mode_write_in_flight = True` for the duration of
        the write and its own dual-check (Phase 8 item 2, part 2) — checked
        by *both* of `_on_event`'s interrupt branches, not just
        `TRANSPORT_MODE_PROPERTY`'s own: real-hardware-confirmed
        (`POCKET_6K_G2 v8.6`, 2026-08-05), leaving `"Output"` mode also
        pushes a `PLAYBACK_PROPERTY` event reporting `speed: 0` as a side
        effect, so this flag has to shield that branch too, not only the
        one it shares a name with. See `_on_event`'s docstring for the full
        finding.
        """
        endpoint = self.profile.rest_endpoint(TRANSPORT_MODE_PROPERTY)
        if endpoint is None or not endpoint.put_supported:
            raise BMDUnsupportedError(
                f"[{self.host}] PUT {TRANSPORT_MODE_PROPERTY} is not confirmed supported in "
                f"the {self.profile.model_key} {self.profile.firmware} rest/ profile — run "
                "tools/rest/probe_endpoints.py --probe-writes against this camera first."
            )
        self._transport_mode_write_in_flight = True
        try:
            self._router.arm(TRANSPORT_MODE_PROPERTY)
            await self._rest_client.put(TRANSPORT_MODE_PROPERTY, {"mode": mode})
            event_value = await self._router.wait_for(
                TRANSPORT_MODE_PROPERTY, timeout=self.verify_timeout_s
            )
            confirmed = _transport_mode(event_value)
            if confirmed is None:
                body = await self._rest_client.get(TRANSPORT_MODE_PROPERTY)
                confirmed = _transport_mode(body)
        finally:
            self._transport_mode_write_in_flight = False
        if confirmed != mode:
            raise BMDVerificationError(
                f"transport mode -> {mode!r}: neither a WS '{TRANSPORT_MODE_PROPERTY}' "
                f"propertyValueChanged event nor a GET readback confirmed mode={mode!r} "
                f"within {self.verify_timeout_s}s"
            )

    async def play(self) -> None:
        """Start normal-speed forward playback — `shuttle(1.0)`, not the
        dedicated `/transports/0/play` trigger. That path is real
        (confirmed present by the read sweep, `docs/rest/transport.md`)
        but has zero write evidence: it sits in `tools/rest/
        probe_endpoints.py`'s `NEVER_WRITE` list (the same position
        `/transports/0/record` was in before Phase 4 proved it out), and
        neither its request body nor a way to verify it has ever been
        captured on real hardware. `/transports/0/playback`, by contrast,
        already has a confirmed `204` same-value `PUT` (design principle
        6's REST sibling) — routing through it here trades a small amount
        of API-surface fidelity to the original plan for a write path this
        session already has real evidence works. This alias itself is
        real-hardware-confirmed (`POCKET_6K_G2` and `POCKET_6K_PRO v8.6`,
        2026-08-04, `examples/rest_playback.py`)."""
        await self.shuttle(1.0)

    async def stop(self) -> None:
        """Stop playback by leaving playback mode entirely — an alias for
        `exit_playback()`, for the same reason `play()` isn't
        `/transports/0/play`: `/transports/0/stop`'s body and verification
        shape are equally unconfirmed, while `exit_playback()`'s body
        (`{"mode": "InputPreview"}`) is sweep-confirmed real. This alias
        itself is real-hardware-confirmed (`POCKET_6K_G2` and
        `POCKET_6K_PRO v8.6`, 2026-08-04, `examples/rest_playback.py`)."""
        await self.exit_playback()

    async def pause(self) -> None:
        """Halt playback at the current position — `shuttle(0.0)`.
        Real-hardware-confirmed (`POCKET_6K_G2` and `POCKET_6K_PRO v8.6`,
        2026-08-04, `examples/rest_playback.py`)."""
        await self.shuttle(0.0)

    async def shuttle(self, speed: float) -> None:
        """`PUT /transports/0/playback` with `speed` merged into the current
        body (see `_put_playback` for the confirmed shape and the
        read-modify-write discipline) — positive shuttles forward, negative
        shuttles backward, magnitude sets the rate; `0.0` pauses at the
        current position. `0.0`/`1.0` (`POCKET_6K_PRO v8.6`, 2026-08-04) and
        `2.0`/`-1.0` (`POCKET_6K_G2` and `POCKET_6K_PRO v8.6`, 2026-08-04,
        `examples/rest_playback.py`'s forward/backward steps) are all
        real-hardware-confirmed; other magnitudes remain an unconfirmed
        extrapolation from the same field. Dual-check verified: a WS
        `propertyValueChanged` event on `PLAYBACK_PROPERTY` primary, a
        `GET` readback secondary, checking the reported body contains
        `{"speed": speed}` via the generic `_contains` helper.
        """
        await self._put_playback({"speed": speed}, action=f"shuttle(speed={speed})")

    async def seek(self, position: int) -> None:
        """`PUT /transports/0/playback` with `position` — playback position
        on the timeline, in video frames — merged into the current body
        (see `_put_playback`). Field name and units are
        real-hardware-confirmed (`POCKET_6K_PRO v8.6`, 2026-08-04;
        `seek(0)` itself real-hardware-confirmed on both cameras,
        2026-08-04, `examples/rest_playback.py`). Same dual-check as
        `shuttle()`.

        Supersedes an earlier, now-disproven hypothesis that this endpoint
        reused `GET /transports/0/timecode`'s own
        `{"timecode": ..., "clip": ...}` field names — the real field is
        `"position"`, an integer frame count, with no separate `clip`
        field at all.
        """
        await self._put_playback({"position": position}, action=f"seek(position={position})")

    async def _put_playback(self, changes: dict[str, Any], *, action: str) -> None:
        """Read-modify-write `/transports/0/playback`: `GET` the current
        body, overlay only `changes`, `PUT` the merged result — mirroring
        `set_camera_format`'s merge discipline (design principle 1) rather
        than sending a bare partial body, so fields this call isn't asked
        to touch keep their last-known value instead of being reset to an
        invented default.

        **Confirmed real body, `POCKET_6K_PRO v8.6`, 2026-08-04**
        (operator testing directly against real hardware — this endpoint's
        first captured *changing* write, distinct from
        `tools/rest/probe_endpoints.py`'s own same-value sweep which only
        proved the endpoint exists):
        `{"type": "Play", "loop": bool, "singleClip": bool,
        "speed": float, "position": int}`. `type: "Play"` covered both a
        paused view (`speed=0.0` — playback view opens, nothing moves) and
        normal forward playback (`speed=1.0`); no other `type` value has
        been observed. This retires the migration plan's earlier
        `"Shuttle"`/`"Jog"` guess for this field as unconfirmed —
        superseded by this real sample, which used `"Play"` for both
        tested speeds. `loop` toggles looping the whole timeline;
        `singleClip` toggles looping just the current clip; `position` is
        documented (by the same real-hardware source) as the playback
        position on the timeline in video frames. `type`/`loop`/
        `singleClip` are not yet exposed as their own parameters here —
        every write through this method leaves them at whatever the
        preceding `GET` reported, via the merge above.

        Verification checks the reported body contains `changes` (the
        fields this call actually asked to change), not the full merged
        body — the initial `GET`'s other fields are context, not something
        this call attests to.

        Holds `_playback_write_in_flight = True` for the duration of the
        write and its own dual-check (Phase 8 item 2, part 2) — checked by
        both of `_on_event`'s interrupt branches, not just
        `PLAYBACK_PROPERTY`'s own, mirroring `_set_transport_mode`'s guard
        widening after the real-hardware finding that a transport-mode
        write can push a side-effect `PLAYBACK_PROPERTY` event. No
        real-hardware evidence yet of the reverse (a `_put_playback()`
        write causing a `TRANSPORT_MODE_PROPERTY` side-effect push), but
        checking both flags here too costs nothing and avoids assuming
        these two write paths are more independent than the one
        real-hardware run actually showed. See `_on_event`'s docstring for
        the full finding.

        On success, if `changes` includes `"speed"`, updates
        `_expected_speed` to that value — the baseline `_on_event`'s
        speed-deviation interrupt check compares future pushes against, so
        the *next* self-requested `shuttle()`/`play()`/`pause()` call is
        judged against what this call actually set, not a stale value from
        before it.
        """
        endpoint = self.profile.rest_endpoint(PLAYBACK_PROPERTY)
        if endpoint is None or not endpoint.put_supported:
            raise BMDUnsupportedError(
                f"[{self.host}] PUT {PLAYBACK_PROPERTY} is not confirmed supported in the "
                f"{self.profile.model_key} {self.profile.firmware} rest/ profile — run "
                "tools/rest/probe_endpoints.py --probe-writes against this camera first."
            )
        current = await self._rest_client.get(PLAYBACK_PROPERTY)
        body = {**current, **changes} if isinstance(current, dict) else dict(changes)
        self._playback_write_in_flight = True
        try:
            self._router.arm(PLAYBACK_PROPERTY)
            await self._rest_client.put(PLAYBACK_PROPERTY, body)
            event_value = await self._router.wait_for(
                PLAYBACK_PROPERTY, timeout=self.verify_timeout_s
            )
            confirmed = _contains(event_value, changes)
            if not confirmed:
                readback = await self._rest_client.get(PLAYBACK_PROPERTY)
                confirmed = _contains(readback, changes)
        finally:
            self._playback_write_in_flight = False
        if not confirmed:
            raise BMDVerificationError(
                f"{action}: neither a WS '{PLAYBACK_PROPERTY}' propertyValueChanged event "
                f"nor a GET readback confirmed {changes} within {self.verify_timeout_s}s"
            )
        if "speed" in changes:
            self._expected_speed = changes["speed"]

    # ── Media device formatting (Phase 10) ──────────────────────────────────

    async def format_device(
        self,
        device_name: str,
        *,
        confirm: bool,
        filesystem: str,
        volume: str | None = None,
        timeout: float = 120.0,
        poll_interval_s: float = 1.0,
    ) -> None:
        """Format a media device — `GET .../doformat` for a one-time `key`,
        then `PUT .../doformat` with `{key, filesystem, volume}` — per the
        official BMD REST spec (`MediaControl.yaml`), the only
        media-erasure capability this API exposes at all. There is no
        per-clip or per-still delete endpoint anywhere in the 11 official
        spec files this codebase has been given (`TimelineControl.yaml`'s
        `DELETE /timelines/0` only clears the timeline *object*, never
        touching clip files on disk — already implemented via
        `select_clip()`). Whether the separate `/mounts/...` filesystem
        surface (`rest/media.py`'s `list_mount`/`path_exists`) supports
        `DELETE` for individual files is a genuinely open question, out of
        scope for this method and deferred by the user's own request — see
        CLAUDE.md's Phase 10 note.

        **This erases every clip and every still on `device_name`,
        irreversibly.** `confirm` has no default — callers must pass
        `confirm=True` explicitly, or this raises `ValueError` before
        sending a single request. This is deliberately stricter than every
        other write in this codebase (none of which gate on an explicit
        confirm flag) because none of them are destructive in this way —
        `record_start`/`set_camera_format`/`select_clip` all change state
        the camera can be asked to change back; a completed format cannot.

        `device_name` is a `StorageDevice.device_name` value (e.g.
        `"sd0"`), sourced from `storage_state()` — not a mount name
        (`mount_names()`'s `"A001-sd1"` style, a different string, see
        `device_info()`'s docstring).

        **Capability check deliberately does not follow `set_camera_format`
        /`_put_playback`'s own pattern of gating on `endpoint.put_supported`.**
        `tools/rest/probe_endpoints.py`'s `NEVER_WRITE` list includes this
        exact path (`/media/devices/{deviceName}/doformat`) — the sweep
        tool refuses to PUT it even with a same-value probe, since there is
        no such thing as a harmless format probe. `put_supported` is
        therefore structurally always `None` for this endpoint; gating on
        it the way every other write here does would make `format_device()`
        permanently unusable regardless of real camera support. This
        method instead gates on `endpoint.supported` — the GET side,
        confirming the endpoint exists and actually returns a format key —
        which is the only sweep-confirmed signal this endpoint can ever
        carry. Real PUT capability rests on the official spec being
        accurate, not on a sweep probe, for this one endpoint only.

        **`filesystem` is a required argument, not the optional one the spec's own
        `MediaControl.yaml` describes it as.** Real-hardware-confirmed,
        `POCKET_6K_G2 v8.6`, 2026-08-13: omitting it (the first version of
        this method, matching the spec literally) got a `400
        {"error": "Field 'filesystem' missing from request body."}` back
        from the camera — the spec's "optional" claim is simply wrong for
        this firmware, real hardware overrides documentation here (design
        principle 6). `filesystem` is validated against a live call to
        `doformat_supported_filesystems()` before any write is attempted,
        raising `BMDUnsupportedError` if the camera doesn't currently offer
        it — the same live-capability-over-hardcoded-assumption discipline
        `set_camera_format` uses for codec/resolution/fps (design
        principle 7's REST sibling). This codebase has no way to read a
        device's *current* filesystem to default to it — `Workingset`'s
        schema has no such field — so there is no safer default than
        requiring the caller to name one explicitly;
        `examples/rest_format_device.py` prints
        `doformat_supported_filesystems()`'s live result before prompting,
        specifically so the operator has real values to choose from rather
        than guessing.

        **`volume` also turned out to be effectively required, not the optional
        field the spec describes either** — a second real-hardware run, same
        camera/firmware/day, with `filesystem` now supplied, got `400
        {"error": "Field 'volume' missing from request body."}` once
        `filesystem` stopped being the blocking field. The first run's
        "`volume` was not rejected" reading was wrong: the camera evidently
        validates fields one at a time and had simply never gotten past
        `filesystem` to check `volume` at all. Unlike `filesystem`, this
        codebase *can* read a device's current volume — `StorageDevice.volume`,
        from `storage_state()` — so `volume: str | None = None` keeps its
        signature and its "omit to keep the current name" behavior, but is
        now resolved for real: if `volume` is not given, this method fetches
        `storage_state()`, finds `device_name`'s entry, and uses its current
        `volume` as the value actually sent — rather than omitting the field
        (which is now known to fail) or guessing a name. If `device_name`
        isn't present in `storage_state()`, or is present with no `volume` of
        its own, this raises `ValueError` before any write — there is no safe
        default left to fall back to at that point.

        **Verification is structurally weaker than every other write in
        this codebase, and this is stated here rather than overstated
        away**: `Notification.yaml`'s `deviceProperty` enum — the complete
        list of WS-subscribable properties — has no entry for any
        `/media/devices/...` path, confirmed by reading the spec directly.
        There is no WS event for format progress or completion at all, so
        design principle 3's "event primary, GET readback secondary"
        dual-check cannot apply here; this method's only verification
        signal is polling `device_info(device_name).state` via `GET`.

        A poll immediately after the `PUT` risks reading a stale `state`
        left over from before the format began (the camera has not
        necessarily transitioned out of `"Mounted"` yet) and treating that
        as a false-positive completion. To guard against exactly that,
        this method requires **observing `state == "Formatting"` at least
        once** before it will accept a later `"Mounted"` reading as genuine
        completion. If `timeout` elapses without observing both a
        `"Formatting"` state and a subsequent terminal state, raises
        `BMDVerificationError`. A terminal state other than `"Mounted"`
        (e.g. `"Uninitialised"`) is also accepted as completion — the spec
        does not promise a freshly formatted device always lands in
        `"Mounted"`, and refusing to recognize a real terminal state here
        would be its own kind of wrong guess about the camera's behavior.

        **Three real-hardware runs, `POCKET_6K_G2 v8.6`, 2026-08-13** (via
        `examples/rest_format_device.py`), the first two of which found and
        fixed the `filesystem`/`volume` defects documented above (both
        `400`s before the camera's format logic ever started — nothing on
        the card touched in either run). **The third run succeeded end to
        end**: `PUT` at `15:37:02.796`, `device_info("sd0").state` observed
        moving `"Mounted"` -> `"Formatting"` -> `"Mounted"`, completion
        logged at `15:37:07.871` — a real full-card format (1TB,
        `filesystem="ExFAT"`, default `volume` resolved to the card's
        existing `"A002"`) in **~5 seconds**, well inside the default
        `timeout=120.0`/`poll_interval_s=1.0`. Confirmed by the resulting
        `storage_state()`: `clip_count` `20` -> `0`, `remaining_space`
        restored to within ~33MB of `total_space` (filesystem overhead).
        This is the first real-hardware confirmation of the
        `"Formatting"`-must-be-observed-first guard actually working as
        designed, not just unit-tested — the guard did not need to reject
        a stale read on this run (the first poll after the `PUT` already
        found `"Formatting"`), so a false-positive scenario this guard
        exists to prevent still has no real-hardware reproduction of its
        own; the guard's *correctness* on the success path is confirmed,
        its necessity is not yet independently demonstrated. See CLAUDE.md's
        Phase 10 note.
        """
        if not confirm:
            raise ValueError(
                "format_device() erases every clip and still on "
                f"{device_name!r} irreversibly — pass confirm=True only once you are "
                "certain that is what you want."
            )

        path = f"/media/devices/{device_name}/doformat"
        endpoint = self.profile.rest_endpoint(path)
        if endpoint is None or not endpoint.supported:
            raise BMDUnsupportedError(
                f"[{self.host}] GET {path} is not confirmed supported in the "
                f"{self.profile.model_key} {self.profile.firmware} rest/ profile — run "
                "tools/rest/probe_endpoints.py against this camera first."
            )

        supported_filesystems = await self.doformat_supported_filesystems()
        if filesystem not in supported_filesystems:
            raise BMDUnsupportedError(
                f"[{self.host}] {device_name}: filesystem {filesystem!r} is not in the "
                f"camera's live doformatSupportedFilesystems {supported_filesystems!r}"
            )

        if volume is None:
            storage = await self.storage_state()
            matching = next((d for d in storage.devices if d.device_name == device_name), None)
            if matching is None or matching.volume is None:
                raise ValueError(
                    f"format_device({device_name!r}): volume not given, and this device's "
                    "current volume name could not be read from storage_state() to default "
                    "to it — pass volume explicitly."
                )
            volume = matching.volume

        key_body = await self._rest_client.get(path)
        key = key_body.get("key") if isinstance(key_body, dict) else None
        if not key:
            raise BMDVerificationError(
                f"[{self.host}] GET {path} returned no format key for {device_name!r} — "
                f"body was {key_body!r}"
            )

        put_body: dict[str, Any] = {"key": key, "filesystem": filesystem, "volume": volume}

        self._log.warning(
            "[%s] Formatting media device %s (filesystem=%s, volume=%s) — irreversible",
            self.host,
            device_name,
            filesystem,
            volume,
        )
        await self._rest_client.put(path, put_body)

        deadline = time.monotonic() + timeout
        seen_formatting = False
        while time.monotonic() < deadline:
            info = await self.device_info(device_name)
            if info.state == "Formatting":
                seen_formatting = True
            elif seen_formatting and info.state != "Formatting":
                self._log.info(
                    "[%s] Format of %s complete — state=%s", self.host, device_name, info.state
                )
                return
            await asyncio.sleep(poll_interval_s)

        raise BMDVerificationError(
            f"[{self.host}] format_device({device_name!r}): did not observe a "
            f"'Formatting' state followed by a terminal state within {timeout}s "
            f"(seen_formatting={seen_formatting})"
        )

    # ── Clip deletion (Phase 11) ────────────────────────────────────────────

    async def delete_clip(self, clip_unique_id: int, *, confirm: bool) -> Clip:
        """Permanently delete one clip from the active storage device via
        `DELETE` on its real `/mounts/...` path — the capability
        `tools/rest/probe_endpoints.py`'s `--probe-mounts-delete`/
        `--delete-real-file` investigation exists to answer (design
        principle 6, `docs/rest/transport.md`'s Mode 3 section).

        **Real-hardware-confirmed working, `POCKET_6K_G2 v8.6`,
        2026-08-13** — but the confirmation is of the underlying
        `GET`/`DELETE`/`GET` sequence, done by hand in Postman after the
        investigation tool itself crashed on the same binary-body defect
        `RestClient.exists()` was already built to avoid (see `decode_body()`
        in `probe_endpoints.py`, and this method's own use of `exists()`
        below for the same reason): `GET` `200`
        (`Content-Type: application/octet-stream`) -> `DELETE` `200 OK` ->
        `GET` `404 Not Found`. This method composes that exact confirmed
        sequence through this session's own machinery
        (`clips()`/`resolve_active_mount()`/`RestClient.exists()`), but has
        not itself been run against real hardware yet — the next real run
        of this method is what closes that specific gap. **Only clip
        deletion is confirmed** — no still's exact `/mounts/...` path has
        been independently confirmed the way this clip's was (the Stills
        directory's own `500` listing defect means one can't be read off a
        listing), so there is no `delete_still()` here. Building one is a
        separate step for once that confirmation exists.

        **This permanently erases the clip, irreversibly.** `confirm` has
        no default — a caller must pass `confirm=True` explicitly, or an
        omitted/`False` value raises `ValueError` before a single request
        is sent, mirroring `format_device()`'s exact gate and for the same
        reason: unlike `record_start`/`set_camera_format`/`select_clip`,
        this changes state the camera cannot be asked to change back.

        `clip_unique_id` is resolved against a fresh `clips()` call,
        raising `ValueError` if it isn't found — the same "never guess
        which clip" discipline `select_clip()` already uses for the same
        situation. Remember `clip_unique_id` is **not stable across
        reconnects** (`docs/rest/session.md`'s `clips()` section) — resolve
        it fresh in the current session, never reuse an id captured earlier.

        **The real `/mounts/...` path is built from a single confirmed
        real-hardware sample, not a general rule**: the one clip this was
        confirmed against, `/mnt/sd0/A002/A002_08120218_C001.braw`
        (`Clip.file_path`, the internal camera path `clips()` reports),
        sits at `/mounts/A002-sd1/A002_08120218_C001.braw` over REST — the
        file directly under the mount root, with the internal path's
        `/A002/` reel subdirectory **not** present in the HTTP layout. This
        method reuses `rest/media.py`'s `resolve_active_mount()` (the
        camera's own real mount listing, never a `deviceName`-to-mount-
        suffix guess — see that module's docstring) for the mount root,
        then appends only `file_path`'s basename — mirroring the one
        confirmed sample exactly. A camera or firmware where clips sit in
        a real subdirectory under the mount root would break this; no
        second data point exists yet to know whether that's ever the case
        (same honesty `resolve_mount_path()`'s own docstring already
        carries for the mount-selection side of this same problem).

        Verification: `RestClient.exists()` before and after the `DELETE`
        — never a plain `get()`, since a clip's body is binary
        (`application/octet-stream`) and `exists()` is specifically built
        to never attempt to parse or decode it (see its docstring — the
        exact class of crash `probe_endpoints.py`'s `request()` hit on
        this same kind of file before being fixed). Raises
        `BMDVerificationError` before sending `DELETE` at all if the
        resolved path doesn't exist yet — the path assumption above was
        wrong, or the clip is already gone — and raises
        `BMDVerificationError` again if the path still exists immediately
        after `DELETE`. No polling loop: the confirmed real sequence
        completed synchronously (no observed "still processing"
        intermediate state), unlike `format_device()`'s multi-second
        full-card format.

        **Real-hardware-confirmed end to end, `POCKET_6K_G2 v8.6`,
        2026-08-13, twice** (`examples/rest_delete_clip.py`, `clip_unique_id`
        `23` then `24`): recorded a real 10s clip, identified it via
        `confirm_new_clip()`, and deleted it — the file-level
        `exists()`/`DELETE`/`exists()` sequence confirmed exactly as
        designed both times, matching the earlier Postman trail precisely.
        **But `GET /clips/list` still reported the clip immediately
        afterward, in the same session, on both runs** — the file was
        genuinely gone (independently confirmed by this method's own
        `exists()` check, the same mechanism Postman verified), yet the
        camera's clip index did not reflect that in the same breath. This
        method's own verification is unaffected — it attests to the
        file's real existence at its real path, not to `/clips/list`'s
        contents, and that attestation was and remains correct.

        **Resolved, not left open: a fresh reconnect clears it.**
        Immediately after the second run, a separate script
        (`examples/rest_read_state.py`) reconnected fresh roughly 48
        seconds later and reported `clips()` and
        `storage_state().active_device.clip_count` both correctly back to
        `1` — matching reality. The staleness is a same-session artifact,
        not a permanent index corruption or a sign the deletion is
        somehow incomplete. Whether it would also clear within the same
        session without reconnecting (immediately, or after some shorter
        delay) remains untested — only "still stale immediately" and
        "correct after a reconnect ~48s later" are actually confirmed.

        `delete_clip()` still makes a best-effort (never-raising,
        `BMDStorageError`-swallowing) check of `clips()` after its own
        confirmation and logs a `WARNING` if the id is still listed —
        informational only, matching design principle 9's "reads are
        best-effort" discipline; it never downgrades or reverses the
        method's own success. A caller that needs `/clips/list` to agree
        immediately should reconnect rather than poll within the same
        session.
        """
        if not confirm:
            raise ValueError(
                "delete_clip() permanently erases this clip from the card — pass "
                "confirm=True only once you are certain that is what you want."
            )

        matches = [clip for clip in await self.clips() if clip.clip_unique_id == clip_unique_id]
        if not matches:
            raise ValueError(
                f"[{self.host}] No clip with clip_unique_id={clip_unique_id} in "
                f"GET /clips/list on {self.profile.model_key} {self.profile.firmware}"
            )
        clip = matches[0]

        mount_path = await resolve_active_mount(self)
        filename = clip.file_path.rsplit("/", 1)[-1]
        target = f"{mount_path}{filename}"

        before = await self._rest_client.exists(target, api_prefixed=False)
        if not before:
            raise BMDVerificationError(
                f"[{self.host}] delete_clip(clip_unique_id={clip_unique_id}): resolved "
                f"path {target!r} does not exist — cannot confirm this clip's real "
                "location before attempting DELETE"
            )

        self._log.warning(
            "[%s] Deleting clip %s (clip_unique_id=%s) — irreversible",
            self.host,
            target,
            clip_unique_id,
        )
        await self._rest_client.delete(target, api_prefixed=False)

        after = await self._rest_client.exists(target, api_prefixed=False)
        if after:
            raise BMDVerificationError(
                f"[{self.host}] delete_clip(clip_unique_id={clip_unique_id}): {target!r} "
                "still exists after DELETE — not confirmed deleted"
            )

        self._log.info(
            "[%s] Clip %s (clip_unique_id=%s) deleted and confirmed gone",
            self.host,
            target,
            clip_unique_id,
        )

        try:
            still_listed = any(c.clip_unique_id == clip_unique_id for c in await self.clips())
        except BMDStorageError:
            still_listed = False
        if still_listed:
            self._log.warning(
                "[%s] clip_unique_id=%s still appears in GET /clips/list immediately "
                "after its file was confirmed deleted from %s — real-hardware-confirmed "
                "transient, POCKET_6K_G2 v8.6, 2026-08-13 (twice): /clips/list does not "
                "reflect a file-level-confirmed deletion within the same session, but a "
                "fresh reconnect ~48s later showed clips()/storage_state().clip_count "
                "both correctly updated. This is informational only and never raises — "
                "the file-level confirmation above is what this method actually attests "
                "to; a caller that needs /clips/list to agree immediately should "
                "reconnect rather than poll within this session.",
                self.host,
                clip_unique_id,
                target,
            )
        return clip

    async def delete_still(self, path: str, *, confirm: bool) -> None:
        """Permanently delete one still from the active storage device via
        `DELETE` on its real `/mounts/.../Stills/...` path.

        **Real-hardware-confirmed working, `POCKET_6K_G2 v8.6`,
        2026-08-13** — done by hand in Postman, the same investigation
        method `delete_clip()`'s own confirmation was built on: `GET`
        `200` -> `DELETE` `200 OK` -> `GET` `404 Not Found`, on a real
        still (`/mounts/A002-sd1/Stills/A002_08120219_S001.braw`). This
        method itself composes that confirmed sequence through
        `RestClient.exists()`/`RestClient.delete()`, but has not itself
        been run against real hardware yet — the next real run of this
        method is what closes that specific gap, the same distinction
        `delete_clip()`'s own docstring drew before its first real run.

        **Unlike `delete_clip()`, this method takes the full `/mounts/...`
        path directly and never tries to resolve or guess one itself.**
        `delete_clip()` can resolve `clip_unique_id` against `clips()`
        because clips have that identifier and a working listing; stills
        have neither — the Stills directory itself `500`s unconditionally
        on listing (`rest/media.py`'s module docstring), so there is no
        `clips()`-equivalent to resolve against and no still-id to accept
        in its place. The existing filename-reconstruction logic
        (`rest/media.py`'s `guess_new_still_path()`) is deliberately
        opt-in and best-effort by design — it was never meant to be a
        source of truth for anything, let alone a destructive target
        (design principle 7's "never guess" discipline, extended here to
        the caller's own responsibility rather than something this method
        could safely do internally). Obtain `path` from
        `guess_new_still_path()` (after independently confirming a photo
        was taken via `wait_for_new_still()`) or from manual
        investigation — the same way the real-hardware confirmation above
        was obtained.

        **This permanently erases the still, irreversibly.** `confirm` has
        no default, mirroring `delete_clip()`/`format_device()`'s exact
        gate — an omitted/`False` value raises `ValueError` before a
        single request is sent.

        Verification: `RestClient.exists()` before and after the `DELETE`
        — never a plain `get()`, since a still's body is binary (a real
        `.dng`/`.braw` image) for the same reason `delete_clip()` avoids
        `get()` for a clip's body. Raises `BMDVerificationError` before
        sending `DELETE` at all if `path` doesn't exist yet, and again if
        it still exists immediately after `DELETE`. No polling loop: the
        confirmed real sequence completed synchronously, the same shape
        `delete_clip()`'s own confirmation showed for a clip file.

        There is no `/clips/list`-equivalent listing for stills to check
        for the kind of same-session staleness `delete_clip()` found and
        now warns about — Stills can't be listed at all, confirmed or
        stale, so no analogous best-effort check is possible here.
        """
        if not confirm:
            raise ValueError(
                "delete_still() permanently erases this still from the card — pass "
                "confirm=True only once you are certain that is what you want."
            )

        before = await self._rest_client.exists(path, api_prefixed=False)
        if not before:
            raise BMDVerificationError(
                f"[{self.host}] delete_still({path!r}): resolved path does not exist — "
                "cannot confirm this still's real location before attempting DELETE"
            )

        self._log.warning("[%s] Deleting still %s — irreversible", self.host, path)
        await self._rest_client.delete(path, api_prefixed=False)

        after = await self._rest_client.exists(path, api_prefixed=False)
        if after:
            raise BMDVerificationError(
                f"[{self.host}] delete_still({path!r}): still exists after DELETE — "
                "not confirmed deleted"
            )

        self._log.info("[%s] Still %s deleted and confirmed gone", self.host, path)
