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
from .timecode import Timecode, decode_rest_timecode

logger = logging.getLogger(__name__)

RECORD_PROPERTY = "/transports/0/record"
FORMAT_PROPERTY = "/system/format"
TRANSPORT_MODE_PROPERTY = "/transports/0"
PLAYBACK_PROPERTY = "/transports/0/playback"
TIMELINE_PATH = "/timelines/0"
TIMELINE_ADD_PATH = "/timelines/0/add"


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
class StorageDevice:
    """One member of `GET /media/workingset`'s fixed-size `workingset`
    array — including empty slots (`device_name == ""`). Never assume
    index 0 is the active device; the working set can (and on real
    hardware does) hold the active disk at a different index."""

    index: int
    device_name: str
    active: bool
    total_space: int
    remaining_space: int = 0
    remaining_record_time: int = 0
    clip_count: int = 0
    volume: str | None = None


@dataclass(frozen=True)
class StorageState:
    """Design principle 10's first real implementation — everything storage-
    aware operations need: card presence (`devices`), the active member
    (`active_device`, resolved from `GET /media/active` rather than by
    guessing an index), remaining space, remaining record time, clip count.
    `active_device` is `None` if no device in the working set reports
    itself active."""

    devices: tuple[StorageDevice, ...]
    active_device: StorageDevice | None


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
        self._log = logging.getLogger(f"{__name__}.{model_key}")

        # `session` may be injected (real or fake) for testing, mirroring
        # RestClient's own constructor — see tests/unit/rest/test_session.py.
        self._session: Any | None = session
        self._owns_session = session is None
        self._client: RestClient | None = None
        self._router = RestEventRouter(on_event=self._on_event)

        # Notification-derived, never inferred from a sent command (design
        # principle 4) — None until the first /transports/0/record event
        # arrives.
        self.is_recording: bool | None = None
        self._recording_stopped = asyncio.Event()

    @property
    def base_url(self) -> str:
        netloc = f"{self.host}:{self.port}" if self.port else self.host
        return f"{self.scheme}://{netloc}"

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
        """Updates `is_recording` only — never inferred from a request this
        session itself made, only from what the camera reports back
        (design principle 4)."""
        if prop != "/transports/0/record" or not isinstance(value, dict):
            return
        recording = value.get("recording")
        if not isinstance(recording, bool):
            return
        self.is_recording = recording
        if recording:
            self._recording_stopped.clear()
        else:
            self._recording_stopped.set()

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
        remaining record time — rather than letting the camera silently
        fail to save the clip."""
        storage = await self.storage_state()
        device = storage.active_device
        if device is None:
            raise BMDStorageError(
                f"[{self.host}] No active storage device in {self.profile.model_key} "
                f"{self.profile.firmware} — cannot start recording"
            )
        if device.remaining_record_time <= 0:
            raise BMDStorageError(
                f"[{self.host}] Active storage device '{device.device_name}' in "
                f"{self.profile.model_key} {self.profile.firmware} has no remaining "
                f"record time ({device.remaining_record_time}) — cannot start recording"
            )

    async def _set_recording_state(self, *, recording: bool) -> None:
        """`PUT /transports/0/record`, verified via the REST dual-check
        design principle 3 specifies: a WS `propertyValueChanged` event
        primary, a `GET` readback secondary. `204` on the `PUT` means
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
        """
        endpoint = self.profile.rest_endpoint(RECORD_PROPERTY)
        if endpoint is None or not endpoint.supported:
            raise BMDUnsupportedError(
                f"[{self.host}] {RECORD_PROPERTY} is not confirmed present in the "
                f"{self.profile.model_key} {self.profile.firmware} rest/ profile — run "
                "tools/rest/probe_endpoints.py against this camera first."
            )

        action = "record_start" if recording else "record_stop"
        self._router.arm(RECORD_PROPERTY)
        await self._rest_client.put(RECORD_PROPERTY, {"recording": recording})
        event_value = await self._router.wait_for(RECORD_PROPERTY, timeout=self.verify_timeout_s)
        confirmed = _recording_flag(event_value)
        if confirmed is None:
            body = await self._rest_client.get(RECORD_PROPERTY)
            confirmed = _recording_flag(body)
        if confirmed != recording:
            raise BMDVerificationError(
                f"{action}: neither a WS '{RECORD_PROPERTY}' propertyValueChanged event "
                f"nor a GET readback confirmed recording={recording} within "
                f"{self.verify_timeout_s}s"
            )

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
        from this run. `resolve_ble_codec_name` (`mapping.py`) can raise
        `BMDUnsupportedError` if a clip's REST codec string isn't in the
        profile's confirmed `format_names` table (no derivation fallback
        — see `mapping.py`'s own docstring for
        why guessing backwards isn't safe); `_resolution_name_for_dimensions`
        can do the same if a clip's pixel dimensions don't match any
        profile `resolutions` entry. Both are real gaps this method
        surfaces loudly rather than papering over, not proof against them.

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
        """
        await self._set_transport_mode("Output")

    async def exit_playback(self) -> None:
        """Leave playback mode, back to live view — `PUT /transports/0
        {"mode": "InputPreview"}`. Real-hardware-confirmed as part of the
        full Phase 7 sequence (`POCKET_6K_G2` and `POCKET_6K_PRO v8.6`,
        2026-08-04, `examples/rest_playback.py`).

        **Possible auto-revert of the camera's format on exit — one data
        point, `POCKET_6K_G2 v8.6`, 2026-08-04.** A run that had
        `select_clip()` switch the camera from `BRaw:8_1 @
        4096x2160p29.97` to a requested clip's own `BRaw:5_1 @
        6144x3456p25` ended with `GET /system/format` reporting
        `BRaw:8_1 @ 4096x2160p29.97` again — the exact pre-`select_clip()`
        format — even though nothing in the script ever requested that
        switch back. This method (and `stop()`, which calls it directly)
        are the only candidates in that run's call sequence for what
        triggered it, but `stop()` *is* this method right now (see
        `stop()`'s own docstring), so this single run cannot distinguish
        "leaving playback mode reverts format" from some other cause tied
        to the same request. Not yet confirmed to repeat, not yet isolated
        to this call specifically, and not relied on anywhere in this
        session — a caller who needs the pre-`select_clip()` format
        restored should not assume this happens and should call
        `set_camera_format()` explicitly instead. See
        docs/rest/session.md's `enter_playback()` / `exit_playback()`
        section for the full writeup.
        """
        await self._set_transport_mode("InputPreview")

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
        """
        endpoint = self.profile.rest_endpoint(TRANSPORT_MODE_PROPERTY)
        if endpoint is None or not endpoint.put_supported:
            raise BMDUnsupportedError(
                f"[{self.host}] PUT {TRANSPORT_MODE_PROPERTY} is not confirmed supported in "
                f"the {self.profile.model_key} {self.profile.firmware} rest/ profile — run "
                "tools/rest/probe_endpoints.py --probe-writes against this camera first."
            )
        self._router.arm(TRANSPORT_MODE_PROPERTY)
        await self._rest_client.put(TRANSPORT_MODE_PROPERTY, {"mode": mode})
        event_value = await self._router.wait_for(
            TRANSPORT_MODE_PROPERTY, timeout=self.verify_timeout_s
        )
        confirmed = _transport_mode(event_value)
        if confirmed is None:
            body = await self._rest_client.get(TRANSPORT_MODE_PROPERTY)
            confirmed = _transport_mode(body)
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
        self._router.arm(PLAYBACK_PROPERTY)
        await self._rest_client.put(PLAYBACK_PROPERTY, body)
        event_value = await self._router.wait_for(PLAYBACK_PROPERTY, timeout=self.verify_timeout_s)
        confirmed = _contains(event_value, changes)
        if not confirmed:
            readback = await self._rest_client.get(PLAYBACK_PROPERTY)
            confirmed = _contains(readback, changes)
        if not confirmed:
            raise BMDVerificationError(
                f"{action}: neither a WS '{PLAYBACK_PROPERTY}' propertyValueChanged event "
                f"nor a GET readback confirmed {changes} within {self.verify_timeout_s}s"
            )
