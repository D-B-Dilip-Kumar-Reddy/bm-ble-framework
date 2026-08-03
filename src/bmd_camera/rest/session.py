"""
bmd_camera/rest/session.py
=============================
RestCameraSession — read-only REST state surface (Phase 3). Composes
`RestClient` (transport) and `RestEventRouter` (WS event buffering) exactly
as `ble/session.py`'s `CameraSession` composes `BMDCameraController` and
`NotificationRouter` — see design principle 5's transport/protocol
boundary, held here for REST.

STATUS: read verbs, record start/stop (Phase 4), and format writes (Phase 5) —
the REST dual-check design principle 3 has always specified: a WS
`propertyValueChanged` event primary, a `GET` readback secondary.

    async with RestCameraSession("172.27.97.141", "POCKET_6K_PRO", "v8.6") as session:
        fmt = await session.get_format()
        storage = await session.storage_state()
        clips = await session.clips()
        tc = await session.timecode()
        await session.record_start()
        await session.record_stop()
        await session.set_camera_format("BRAW", "5:1", "4K DCI", "23.98")

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
from .constants import WS_PATH
from .events import RestEventRouter
from .exceptions import BMDRestError
from .mapping import resolve_rest_codec_name
from .timecode import Timecode, decode_rest_timecode

logger = logging.getLogger(__name__)

RECORD_PROPERTY = "/transports/0/record"
FORMAT_PROPERTY = "/system/format"


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
            await self._router.subscribe("/transports/0/record")
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
