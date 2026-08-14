"""Unit tests for :mod:`bmd_camera.rest.session`.

No real network — read-verb tests inject a fake `RestClient`-like object
directly (bypassing `RestCameraSession.__init__`'s real `CameraProfile.for_model`
lookup via `__new__`, the same pattern `tests/unit/test_session.py` uses for
the BLE `CameraSession`); connection-lifecycle tests inject a combined fake
`aiohttp.ClientSession` supporting both `RestClient`'s `.request()` and
`RestEventRouter`'s `.ws_connect()`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp
import pytest

from bmd_camera.camera_profile import CameraProfile
from bmd_camera.exceptions import (
    BMDConnectionError,
    BMDStorageError,
    BMDUnsupportedError,
    BMDVerificationError,
)
from bmd_camera.rest.events import RestEventRouter
from bmd_camera.rest.exceptions import BMDRestError
from bmd_camera.rest.session import (
    FORMAT_PROPERTY,
    PLAY_PROPERTY,
    PLAYBACK_PROPERTY,
    RECORD_PROPERTY,
    STOP_PROPERTY,
    TIMELINE_ADD_PATH,
    TIMELINE_PATH,
    TRANSPORT_MODE_PROPERTY,
    WORKINGSET_PROPERTY,
    BulkDeleteResult,
    Clip,
    Format,
    RestCameraSession,
    SupportedFormat,
)
from bmd_camera.rest.state import CameraState, StorageDevice, StorageState

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v8.6"


def make_profile(*, rest_raw: dict | None = None) -> CameraProfile:
    ble_raw = {"_meta": {"model": "Pocket 6K G2", "ble_name": "A:TEST"}}
    profile = CameraProfile._from_raw(MODEL_KEY, FIRMWARE, ble_raw)
    if rest_raw is not None:
        profile.rest = CameraProfile._rest_from_raw(rest_raw)
    return profile


def make_format_profile(
    *,
    format_names: dict | None = None,
    format_endpoint_confirmed: bool = True,
    supported_formats_confirmed: bool = True,
) -> CameraProfile:
    """A profile carrying the codecs/resolutions/fps_modes tables
    set_camera_format's local validation consumes (shared with BLE — design
    principle 1, mirrors tests/unit/test_session.py's make_settings_profile),
    plus a rest/ file confirming /system/format's PUT side and
    /system/supportedFormats' GET side were swept — both required before
    set_camera_format will attempt a write."""
    ble_raw = {
        "_meta": {"model": "Pocket 6K G2", "ble_name": "A:TEST"},
        "codecs": {
            "BRAW": {"id": 3, "variants": {"Q0": 0, "5:1": 3}},
            "ProRes": {"id": 2, "variants": {"HQ": 0, "422": 1}},
        },
        "resolutions": {
            "4K DCI": {"width": 4096, "height": 2160, "codecs": ["BRAW", "ProRes"]},
            "HD": {"width": 1920, "height": 1080, "codecs": ["ProRes"]},
        },
        "fps_modes": {
            "23.98": {"fps_int": 24, "m_rate": 1, "frame_flags": 19},
            "24": {"fps_int": 24, "m_rate": 0, "frame_flags": 0},
        },
    }
    profile = CameraProfile._from_raw(MODEL_KEY, FIRMWARE, ble_raw)

    endpoints: dict[str, dict] = {}
    if format_endpoint_confirmed:
        endpoints[FORMAT_PROPERTY] = {
            "status": 200,
            "supported": True,
            "put_status": 204,
            "put_supported": True,
        }
    if supported_formats_confirmed:
        endpoints["/system/supportedFormats"] = {"status": 200, "supported": True}

    rest_raw = {
        "_meta": {"model_key": MODEL_KEY, "firmware": FIRMWARE, "status": "UNVERIFIED"},
        "endpoints": endpoints,
        "format_names": format_names or {},
    }
    profile.rest = CameraProfile._rest_from_raw(rest_raw)
    return profile


CURRENT_FORMAT_BODY = {
    "codec": "ProRes:Proxy",
    "frameRate": "24",
    "maxOffSpeedFrameRate": 60,
    "minOffSpeedFrameRate": 5,
    "offSpeedEnabled": False,
    "offSpeedFrameRate": 24,
    "recordResolution": {"height": 1080, "width": 1920},
    "sensorResolution": {"height": 3024, "width": 5744},
}

SUPPORTED_FORMATS_BODY_BRAW_4K_DCI = {
    "supportedFormats": [
        {
            "codecs": ["BRaw:Q0", "BRaw:5_1"],
            "frameRates": ["23.98", "24"],
            "recordResolution": {"width": 4096, "height": 2160},
            "sensorResolution": {"width": 4096, "height": 2160},
        }
    ]
}


def make_profile_with_record_confirmed() -> CameraProfile:
    """A profile whose rest/ file confirms /transports/0/record's GET side
    was swept — probe_endpoints.py never PUTs this path (NEVER_WRITE), so
    only the GET side can ever be profile-confirmed; see
    RestCameraSession._set_recording_state's docstring."""
    rest_raw = {
        "_meta": {"model_key": MODEL_KEY, "firmware": FIRMWARE, "status": "UNVERIFIED"},
        "endpoints": {RECORD_PROPERTY: {"status": 200, "supported": True}},
    }
    return make_profile(rest_raw=rest_raw)


def make_playback_profile() -> CameraProfile:
    """A profile whose rest/ file confirms TRANSPORT_MODE_PROPERTY's and
    PLAYBACK_PROPERTY's PUT side (both same-value-probed real,
    docs/rest/transport.md), plus TIMELINE_PATH's GET side only — DELETE/POST
    are NEVER_WRITE, so only GET can ever be profile-confirmed for it, the
    same shape as RECORD_PROPERTY (make_profile_with_record_confirmed)."""
    rest_raw = {
        "_meta": {"model_key": MODEL_KEY, "firmware": FIRMWARE, "status": "UNVERIFIED"},
        "endpoints": {
            TRANSPORT_MODE_PROPERTY: {
                "status": 200,
                "supported": True,
                "put_status": 204,
                "put_supported": True,
            },
            PLAYBACK_PROPERTY: {
                "status": 200,
                "supported": True,
                "put_status": 204,
                "put_supported": True,
            },
            TIMELINE_PATH: {"status": 200, "supported": True},
        },
    }
    return make_profile(rest_raw=rest_raw)


# ── select_clip() fixtures — real evidence, POCKET_6K_PRO v8.6, 2026-08-04 ──
# Clip 1 (ProRes:Proxy, 4096x2160p24) is the exact clip from the Postman
# debugging session that established select_clip()'s whole design: the
# camera's playable timeline is always every clip matching current format,
# never just the requested clip_unique_id.

CLIP_1_BODY = {
    "clipUniqueId": 1,
    "filePath": "/mnt/sd0/A001/A001_07311253_C001.mov",
    "codecFormat": {"codec": "ProRes:Proxy", "container": "MOV"},
    "startTimecode": "12:53:56:01",
    "durationTimecode": "00:00:02:12",
    "videoFormat": "4096x2160p24",
}

MATCHING_FORMAT_BODY = {
    "codec": "ProRes:Proxy",
    "frameRate": "24",
    "maxOffSpeedFrameRate": 60,
    "minOffSpeedFrameRate": 5,
    "offSpeedEnabled": False,
    "offSpeedFrameRate": 24,
    "recordResolution": {"height": 2160, "width": 4096},
    "sensorResolution": {"height": 3024, "width": 5744},
}

SUPPORTED_FORMATS_BODY_PRORES_PROXY_4K_DCI = {
    "supportedFormats": [
        {
            "codecs": ["ProRes:Proxy", "ProRes:LT", "ProRes:Original", "ProRes:HQ"],
            "frameRates": ["23.98", "24", "25"],
            "recordResolution": {"width": 4096, "height": 2160},
            "sensorResolution": {"width": 5744, "height": 3024},
        }
    ]
}


def make_select_clip_profile(
    *,
    format_names: dict | None = None,
    resolutions: dict | None = None,
    timeline_confirmed: bool = True,
) -> CameraProfile:
    """A profile carrying everything select_clip() touches: the shared
    codecs/resolutions/fps_modes tables set_camera_format() validates
    against (mirrors make_format_profile), plus a rest/ file confirming
    /system/format's PUT side, /system/supportedFormats' GET side, and
    /timelines/0's GET side, with a format_names table mapping ProRes/Proxy
    to its real REST spelling — needed by resolve_ble_codec_name's reverse
    lookup, the direction set_camera_format's own tests never exercise."""
    ble_raw = {
        "_meta": {"model": "Pocket 6K Pro", "ble_name": "A:TEST"},
        "codecs": {"ProRes": {"id": 2, "variants": {"Proxy": 0, "422": 1}}},
        "resolutions": (
            resolutions
            if resolutions is not None
            else {"4K DCI": {"width": 4096, "height": 2160, "codecs": ["ProRes"]}}
        ),
        "fps_modes": {"24": {"fps_int": 24, "m_rate": 0, "frame_flags": 0}},
    }
    profile = CameraProfile._from_raw(MODEL_KEY, FIRMWARE, ble_raw)

    endpoints: dict[str, dict] = {
        FORMAT_PROPERTY: {
            "status": 200,
            "supported": True,
            "put_status": 204,
            "put_supported": True,
        },
        "/system/supportedFormats": {"status": 200, "supported": True},
    }
    if timeline_confirmed:
        endpoints[TIMELINE_PATH] = {"status": 200, "supported": True}

    rest_raw = {
        "_meta": {"model_key": MODEL_KEY, "firmware": FIRMWARE, "status": "UNVERIFIED"},
        "endpoints": endpoints,
        "format_names": (
            format_names if format_names is not None else {"ProRes": {"Proxy": "ProRes:Proxy"}}
        ),
    }
    profile.rest = CameraProfile._rest_from_raw(rest_raw)
    return profile


class FakeRestClient:
    """Minimal stand-in for RestClient: `.get(path)` returns a canned body,
    or raises a canned exception (for `errors`) — e.g. simulating a real
    404 without a real network. `.put(path, body)` records the call and
    returns a canned response (None by default, matching a real 204)."""

    def __init__(
        self,
        responses: dict[str, object],
        *,
        errors: dict[str, Exception] | None = None,
        put_responses: dict[str, object] | None = None,
        exists_responses: dict[str, bool] | None = None,
        download_responses: dict[str, bytes] | None = None,
    ):
        self.responses = responses
        self.errors = errors or {}
        self.put_responses = put_responses or {}
        self.exists_responses = exists_responses or {}
        self.download_responses = download_responses or {}
        self.calls: list[str] = []
        self.put_calls: list[tuple[str, object]] = []
        self.exists_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.post_calls: list[tuple[str, object]] = []
        self.download_calls: list[tuple[str, str]] = []
        self.api_prefixed_calls: dict[str, bool] = {}

    async def get(self, path: str, *, api_prefixed: bool = True):
        self.calls.append(path)
        self.api_prefixed_calls[path] = api_prefixed
        if path in self.errors:
            raise self.errors[path]
        return self.responses[path]

    async def put(self, path: str, body: object):
        self.put_calls.append((path, body))
        if path in self.errors:
            raise self.errors[path]
        return self.put_responses.get(path)

    async def delete(self, path: str, *, api_prefixed: bool = True):
        self.delete_calls.append(path)
        self.api_prefixed_calls[path] = api_prefixed
        if path in self.errors:
            raise self.errors[path]
        return None

    async def post(self, path: str, body: object):
        self.post_calls.append((path, body))
        if path in self.errors:
            raise self.errors[path]
        return None

    async def exists(self, path: str, *, api_prefixed: bool = True) -> bool:
        self.exists_calls.append(path)
        self.api_prefixed_calls[path] = api_prefixed
        if path in self.errors:
            raise self.errors[path]
        return self.exists_responses.get(path, False)

    async def download(self, path: str, dest, *, api_prefixed: bool = False) -> int:
        self.download_calls.append((path, str(dest)))
        self.api_prefixed_calls[path] = api_prefixed
        if path in self.errors:
            raise self.errors[path]
        content = self.download_responses.get(path, b"")
        Path(dest).write_bytes(content)
        return len(content)


def make_session(
    profile: CameraProfile, *, client: FakeRestClient | None = None
) -> RestCameraSession:
    """Build a RestCameraSession bypassing __init__'s real profile lookup,
    mirroring tests/unit/test_session.py's make_session() for BLE."""
    session = RestCameraSession.__new__(RestCameraSession)
    session.profile = profile
    session.host = "cam.local"
    session.scheme = "http"
    session.port = None
    session.timeout_s = 5.0
    session.ws_timeout_s = 5.0
    session.verify_timeout_s = 5.0
    session.stop_verify_timeout_s = 5.0 * 3
    session._log = logging.getLogger("test.rest_session")
    session._session = None
    session._owns_session = True
    session._client = client
    session._router = RestEventRouter(on_event=session._on_event)
    session.state = CameraState()
    session._recording_stopped = asyncio.Event()
    session._low_storage_min_record_time_s = None
    session._low_storage_min_space_bytes = None
    session._low_storage_event = asyncio.Event()
    session._playback_write_in_flight = False
    session._transport_mode_write_in_flight = False
    session._expected_speed = None
    return session


class TestCameraStateDelegation:
    """Phase 9: is_recording/last_known_storage/_in_playback/last_known_play/
    last_known_stop/playback_interrupted moved onto CameraState, exposed via
    identically-named properties on RestCameraSession. Confirms the
    delegation is real in both directions, and that playback_interrupted
    (a mutable asyncio.Event, never reassigned) has no setter."""

    def test_is_recording_round_trips_through_state(self):
        session = make_session(make_profile())

        session.is_recording = True

        assert session.state.is_recording is True
        session.state.is_recording = False
        assert session.is_recording is False

    def test_last_known_storage_round_trips_through_state(self):
        session = make_session(make_profile())
        storage = StorageState(devices=(), active_device=None)

        session.last_known_storage = storage

        assert session.state.last_known_storage is storage

    def test_in_playback_round_trips_through_state(self):
        session = make_session(make_profile())

        session._in_playback = True

        assert session.state._in_playback is True

    def test_last_known_play_and_stop_round_trip_through_state(self):
        session = make_session(make_profile())

        session.last_known_play = True
        session.last_known_stop = False

        assert session.state.last_known_play is True
        assert session.state.last_known_stop is False

    def test_playback_interrupted_getter_returns_state_event(self):
        session = make_session(make_profile())

        assert session.playback_interrupted is session.state.playback_interrupted

    def test_playback_interrupted_has_no_setter(self):
        session = make_session(make_profile())

        with pytest.raises(AttributeError):
            session.playback_interrupted = asyncio.Event()


class TestGetFormat:
    @pytest.mark.asyncio
    async def test_parses_real_shape(self):
        client = FakeRestClient(
            {
                "/system/format": {
                    "codec": "ProRes:Proxy",
                    "frameRate": "23.98",
                    "maxOffSpeedFrameRate": 60,
                    "minOffSpeedFrameRate": 5,
                    "offSpeedEnabled": False,
                    "offSpeedFrameRate": 24,
                    "recordResolution": {"height": 2160, "width": 4096},
                    "sensorResolution": {"height": 3024, "width": 5744},
                }
            }
        )
        session = make_session(make_profile(), client=client)

        fmt = await session.get_format()

        assert fmt == Format(
            codec="ProRes:Proxy",
            frame_rate="23.98",
            record_resolution=(4096, 2160),
            sensor_resolution=(5744, 3024),
            off_speed_enabled=False,
            off_speed_frame_rate=24,
            min_off_speed_frame_rate=5,
            max_off_speed_frame_rate=60,
        )
        assert client.calls == ["/system/format"]


class TestSupportedFormats:
    @pytest.mark.asyncio
    async def test_raises_when_not_confirmed_in_profile(self):
        session = make_session(make_profile(), client=FakeRestClient({}))

        with pytest.raises(BMDUnsupportedError, match="supportedFormats"):
            await session.supported_formats()

    @pytest.mark.asyncio
    async def test_raises_when_profile_marks_it_unsupported(self):
        rest_raw = {
            "_meta": {"model_key": MODEL_KEY, "firmware": FIRMWARE, "status": "UNVERIFIED"},
            "endpoints": {"/system/supportedFormats": {"status": 501, "supported": False}},
        }
        session = make_session(make_profile(rest_raw=rest_raw), client=FakeRestClient({}))

        with pytest.raises(BMDUnsupportedError):
            await session.supported_formats()

    @pytest.mark.asyncio
    async def test_parses_confirmed_endpoint(self):
        rest_raw = {
            "_meta": {"model_key": MODEL_KEY, "firmware": FIRMWARE, "status": "UNVERIFIED"},
            "endpoints": {"/system/supportedFormats": {"status": 200, "supported": True}},
        }
        client = FakeRestClient(
            {
                "/system/supportedFormats": {
                    "supportedFormats": [
                        {
                            "codecs": ["BRaw:Q0", "BRaw:Q1"],
                            "frameRates": ["23.98", "24"],
                            "maxOffSpeedFrameRate": 60,
                            "minOffSpeedFrameRate": 5,
                            "recordResolution": {"height": 2160, "width": 3840},
                            "sensorResolution": {"height": 2160, "width": 3840},
                        }
                    ]
                }
            }
        )
        session = make_session(make_profile(rest_raw=rest_raw), client=client)

        formats = await session.supported_formats()

        assert formats == (
            SupportedFormat(
                codecs=("BRaw:Q0", "BRaw:Q1"),
                frame_rates=("23.98", "24"),
                record_resolution=(3840, 2160),
                sensor_resolution=(3840, 2160),
                min_off_speed_frame_rate=5,
                max_off_speed_frame_rate=60,
            ),
        )


class TestStorageState:
    @pytest.mark.asyncio
    async def test_combines_workingset_and_active(self):
        client = FakeRestClient(
            {
                "/media/workingset": {
                    "size": 3,
                    "workingset": [
                        {
                            "activeDisk": False,
                            "clipCount": 0,
                            "deviceName": "",
                            "index": 0,
                            "remainingRecordTime": 0,
                            "remainingSpace": 0,
                            "totalSpace": 0,
                        },
                        {
                            "activeDisk": True,
                            "clipCount": 1,
                            "deviceName": "sd0",
                            "index": 1,
                            "remainingRecordTime": 52284,
                            "remainingSpace": 1023925420032,
                            "totalSpace": 1024060293120,
                            "volume": "A001",
                        },
                    ],
                },
                "/media/active": {"deviceName": "sd0", "workingsetIndex": 1},
            }
        )
        session = make_session(make_profile(), client=client)

        storage = await session.storage_state()

        assert isinstance(storage, StorageState)
        assert storage.active_device is not None
        assert storage.active_device.index == 1
        assert storage.active_device.device_name == "sd0"
        assert storage.active_device.remaining_space == 1023925420032
        assert len(storage.devices) == 2

    @pytest.mark.asyncio
    async def test_no_active_device_when_none_reports_active(self):
        client = FakeRestClient(
            {
                "/media/workingset": {
                    "size": 1,
                    "workingset": [
                        {
                            "activeDisk": False,
                            "clipCount": 0,
                            "deviceName": "",
                            "index": 0,
                            "remainingRecordTime": 0,
                            "remainingSpace": 0,
                            "totalSpace": 0,
                        }
                    ],
                },
                "/media/active": {"deviceName": "", "workingsetIndex": -1},
            }
        )
        session = make_session(make_profile(), client=client)

        storage = await session.storage_state()

        assert storage.active_device is None


class TestClips:
    @pytest.mark.asyncio
    async def test_parses_clip_list_key_not_clips(self):
        """Real evidence: the key is clipList, not clips
        (docs/rest/transport.md)."""
        client = FakeRestClient(
            {
                "/clips/list": {
                    "clipList": [
                        {
                            "clipUniqueId": 1,
                            "filePath": "/mnt/sd0/A001/A001_07311253_C001.mov",
                            "codecFormat": {"codec": "ProRes:Proxy", "container": "MOV"},
                            "startTimecode": "12:53:56:01",
                            "durationTimecode": "00:00:02:12",
                            "videoFormat": "4096x2160p24",
                        }
                    ]
                }
            }
        )
        session = make_session(make_profile(), client=client)

        clips = await session.clips()

        assert clips == (
            Clip(
                clip_unique_id=1,
                file_path="/mnt/sd0/A001/A001_07311253_C001.mov",
                codec="ProRes:Proxy",
                container="MOV",
                start_timecode="12:53:56:01",
                duration_timecode="00:00:02:12",
                video_format="4096x2160p24",
            ),
        )

    @pytest.mark.asyncio
    async def test_empty_clip_list(self):
        client = FakeRestClient({"/clips/list": {"clipList": []}})
        session = make_session(make_profile(), client=client)

        assert await session.clips() == ()

    @pytest.mark.asyncio
    async def test_no_media_404_raises_bmd_storage_error(self):
        """Real-hardware-confirmed (POCKET_6K_G2 v8.6, 2026-08-03): with no
        SD card inserted, /clips/list returns 404 {"error": "No disk or
        media"} rather than an empty clipList. Must surface as
        BMDStorageError — design principle 10's "no storage media" case —
        not a misleading empty tuple or a generic BMDRestError."""
        error = BMDRestError(
            "[cam.local] GET /clips/list -> 404: {'error': 'No disk or media'}",
            status=404,
            body={"error": "No disk or media"},
        )
        client = FakeRestClient({}, errors={"/clips/list": error})
        session = make_session(make_profile(), client=client)

        with pytest.raises(BMDStorageError, match="No storage media"):
            await session.clips()

    @pytest.mark.asyncio
    async def test_non_404_rest_error_propagates_unchanged(self):
        """Only a 404 is interpreted as "no media" — any other BMDRestError
        (e.g. a real 500 firmware defect) must not be silently reclassified
        as a storage condition."""
        error = BMDRestError("[cam.local] GET /clips/list -> 500: {}", status=500, body={})
        client = FakeRestClient({}, errors={"/clips/list": error})
        session = make_session(make_profile(), client=client)

        with pytest.raises(BMDRestError, match="500"):
            await session.clips()


class TestTimecode:
    @pytest.mark.asyncio
    async def test_decodes_real_captured_value(self):
        client = FakeRestClient({"/transports/0/timecode": {"clip": 0, "timecode": 274153986}})
        session = make_session(make_profile(), client=client)

        tc = await session.timecode()

        assert (tc.hours, tc.minutes, tc.seconds, tc.frames) == (10, 57, 42, 2)


class TestClipTimecode:
    @pytest.mark.asyncio
    async def test_decodes_real_captured_value(self):
        """clip=258 == 0x00000102 == 00:00:01:02 — a real value captured
        moments after record_start on POCKET_6K_G2 v8.6, 2026-08-05 (the
        Alert-mode stress-test run, docs/rest/session.md). Confirms clip
        decodes with the exact same BCD function as timecode's own field,
        per the Notification.yaml spec."""
        client = FakeRestClient({"/transports/0/timecode": {"clip": 258, "timecode": 0}})
        session = make_session(make_profile(), client=client)

        tc = await session.clip_timecode()

        assert (tc.hours, tc.minutes, tc.seconds, tc.frames) == (0, 0, 1, 2)


class TestMountNames:
    @pytest.mark.asyncio
    async def test_parses_real_shape(self):
        """GET /mounts/ real shape (docs/rest/transport.md): a list of
        {"name": ..., "type": ...} entries — only directories count.
        Also asserts api_prefixed=False is passed through to the client —
        /mounts/ is the Web Media Manager, outside /control/api/v1; a real
        run without this flag 404s (see RestClient's module docstring)."""
        client = FakeRestClient(
            {
                "/mounts/": [
                    {"name": "A001-sd1", "type": "directory", "mtime": "Fri, 31 Jul 2026 12:54:20"}
                ]
            }
        )
        session = make_session(make_profile(), client=client)

        assert await session.mount_names() == ("A001-sd1",)
        assert client.api_prefixed_calls["/mounts/"] is False

    @pytest.mark.asyncio
    async def test_ignores_non_directory_entries(self):
        client = FakeRestClient(
            {
                "/mounts/": [
                    {"name": "A001-sd1", "type": "directory"},
                    {"name": "readme.txt", "type": "file"},
                ]
            }
        )
        session = make_session(make_profile(), client=client)

        assert await session.mount_names() == ("A001-sd1",)

    @pytest.mark.asyncio
    async def test_empty_when_no_mounts(self):
        client = FakeRestClient({"/mounts/": []})
        session = make_session(make_profile(), client=client)

        assert await session.mount_names() == ()


class TestListMount:
    """`mount_names()` is now built on `list_mount()` — these tests cover
    `list_mount()` directly, including the shape `rest/media.py`'s
    `stills_marker()` relies on: a mount root's `Stills` entry carrying its
    own `mtime`, which advances whenever a file is added inside it without
    ever needing to list Stills' own contents (permanently `500`s —
    docs/rest/transport.md)."""

    @pytest.mark.asyncio
    async def test_returns_raw_entries(self):
        client = FakeRestClient(
            {
                "/mounts/A001-sd1/": [
                    {"name": "Stills", "type": "directory", "mtime": "Fri, 31 Jul 2026 12:54:20"},
                    {
                        "name": "A001_07311253_C001.mov",
                        "type": "file",
                        "mtime": "Fri, 31 Jul 2026 12:53:58",
                        "size": 49058872,
                    },
                ]
            }
        )
        session = make_session(make_profile(), client=client)

        entries = await session.list_mount("/mounts/A001-sd1/")

        assert entries == (
            {"name": "Stills", "type": "directory", "mtime": "Fri, 31 Jul 2026 12:54:20"},
            {
                "name": "A001_07311253_C001.mov",
                "type": "file",
                "mtime": "Fri, 31 Jul 2026 12:53:58",
                "size": 49058872,
            },
        )
        assert client.api_prefixed_calls["/mounts/A001-sd1/"] is False

    @pytest.mark.asyncio
    async def test_empty_when_no_entries(self):
        client = FakeRestClient({"/mounts/A001-sd1/": []})
        session = make_session(make_profile(), client=client)

        assert await session.list_mount("/mounts/A001-sd1/") == ()

    @pytest.mark.asyncio
    async def test_ignores_non_dict_entries(self):
        client = FakeRestClient({"/mounts/A001-sd1/": ["not-a-dict", 42]})
        session = make_session(make_profile(), client=client)

        assert await session.list_mount("/mounts/A001-sd1/") == ()


class TestPathExists:
    """Reintroduced alongside RestClient.exists() for rest/media.py's
    guess_new_still_path() — an opt-in, best-effort filename lookup, never
    part of wait_for_new_still()'s actual confirmation."""

    @pytest.mark.asyncio
    async def test_delegates_to_client_exists(self):
        client = FakeRestClient(
            {}, exists_responses={"/mounts/A001-sd1/Stills/A001_0001_S001.dng": True}
        )
        session = make_session(make_profile(), client=client)

        assert await session.path_exists("/mounts/A001-sd1/Stills/A001_0001_S001.dng") is True
        assert await session.path_exists("/mounts/A001-sd1/Stills/A001_0001_S999.dng") is False
        assert client.exists_calls == [
            "/mounts/A001-sd1/Stills/A001_0001_S001.dng",
            "/mounts/A001-sd1/Stills/A001_0001_S999.dng",
        ]
        # /mounts/... is outside /control/api/v1 — see TestListMount.test_returns_raw_entries
        assert client.api_prefixed_calls["/mounts/A001-sd1/Stills/A001_0001_S001.dng"] is False
        assert client.api_prefixed_calls["/mounts/A001-sd1/Stills/A001_0001_S999.dng"] is False


class TestNotConnected:
    @pytest.mark.asyncio
    async def test_read_verb_raises_before_connect(self):
        session = make_session(make_profile(), client=None)

        with pytest.raises(BMDConnectionError, match="not connected"):
            await session.get_format()


class TestIsRecordingTracking:
    def test_updates_from_record_event(self):
        session = make_session(make_profile())

        session._on_event("/transports/0/record", {"recording": True})
        assert session.is_recording is True

        session._on_event("/transports/0/record", {"recording": False})
        assert session.is_recording is False

    def test_ignores_unrelated_property(self):
        session = make_session(make_profile())

        session._on_event("/system/format", {"codec": "ProRes:Proxy"})

        assert session.is_recording is None

    def test_ignores_malformed_value(self):
        session = make_session(make_profile())

        session._on_event("/transports/0/record", {"recording": "not a bool"})
        session._on_event("/transports/0/record", None)

        assert session.is_recording is None


# Real /media/workingset propertyValueChanged body, POCKET_6K_G2 v8.6,
# 2026-08-05 (tools/rest/watch_events.py, mid-recording) — see
# docs/rest/session.md's is_recording/wait_while_recording() section.
REAL_WORKINGSET_EVENT = {
    "size": 3,
    "workingset": [
        {
            "activeDisk": False,
            "clipCount": 0,
            "deviceName": "",
            "index": 0,
            "remainingRecordTime": 0,
            "remainingSpace": 0,
            "totalSpace": 0,
        },
        {
            "activeDisk": True,
            "clipCount": 9,
            "deviceName": "sd0",
            "index": 1,
            "remainingRecordTime": 13107,
            "remainingSpace": 943720932608,
            "totalSpace": 1024060293120,
            "volume": "A002",
        },
        {
            "activeDisk": False,
            "clipCount": 0,
            "deviceName": "",
            "index": 2,
            "remainingRecordTime": 0,
            "remainingSpace": 0,
            "totalSpace": 0,
        },
    ],
}


class TestLastKnownStorageTracking:
    def test_updates_from_workingset_event(self):
        session = make_session(make_profile())

        session._on_event(WORKINGSET_PROPERTY, REAL_WORKINGSET_EVENT)

        assert session.last_known_storage is not None
        device = session.last_known_storage.active_device
        assert device is not None
        assert (device.device_name, device.remaining_record_time, device.remaining_space) == (
            "sd0",
            13107,
            943720932608,
        )

    def test_ignores_unrelated_property(self):
        session = make_session(make_profile())

        session._on_event("/transports/0/record", {"recording": True})

        assert session.last_known_storage is None

    def test_ignores_malformed_value(self):
        session = make_session(make_profile())

        session._on_event(WORKINGSET_PROPERTY, "not a dict")
        session._on_event(WORKINGSET_PROPERTY, None)

        assert session.last_known_storage is None


def _storage_client(
    *,
    active: bool,
    remaining_record_time: int,
    remaining_space: int = 123,
    extra_responses: dict[str, object] | None = None,
) -> FakeRestClient:
    device = {
        "activeDisk": active,
        "clipCount": 0,
        "deviceName": "sd0" if active else "",
        "index": 0,
        "remainingRecordTime": remaining_record_time,
        "remainingSpace": remaining_space,
        "totalSpace": 456,
        "volume": "A001" if active else None,
    }
    responses = {
        "/media/workingset": {"size": 1, "workingset": [device]},
        "/media/active": {
            "deviceName": device["deviceName"],
            "workingsetIndex": 0 if active else -1,
        },
        **(extra_responses or {}),
    }
    return FakeRestClient(responses)


class TestRecordStart:
    @pytest.mark.asyncio
    async def test_raises_bmd_storage_error_when_no_active_device(self):
        client = _storage_client(active=False, remaining_record_time=0)
        session = make_session(make_profile_with_record_confirmed(), client=client)

        with pytest.raises(BMDStorageError, match="No active storage device"):
            await session.record_start()

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_raises_bmd_storage_error_when_no_remaining_space(self):
        client = _storage_client(active=True, remaining_record_time=100, remaining_space=0)
        session = make_session(make_profile_with_record_confirmed(), client=client)

        with pytest.raises(BMDStorageError, match="no remaining space"):
            await session.record_start()

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_does_not_raise_when_remaining_record_time_is_stale_zero(self):
        """Phase 9 fix: remaining_record_time is confirmed stale immediately
        after a format switch (docs/rest/session.md) — _require_storage_ready()
        no longer gates on it. A stale remaining_record_time=0 alongside a
        healthy remaining_space must not block record_start()."""
        client = _storage_client(active=True, remaining_record_time=0, remaining_space=123)
        session = make_session(make_profile_with_record_confirmed(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": RECORD_PROPERTY,
                        "value": {"recording": True},
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.record_start()

        assert client.put_calls == [(RECORD_PROPERTY, {"recording": True})]

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_endpoint_not_confirmed_in_profile(self):
        client = _storage_client(active=True, remaining_record_time=100)
        session = make_session(make_profile(), client=client)  # no rest_raw

        with pytest.raises(BMDUnsupportedError, match="transports/0/record"):
            await session.record_start()

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_confirmed_by_ws_event_primary(self):
        client = _storage_client(active=True, remaining_record_time=100)
        session = make_session(make_profile_with_record_confirmed(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": RECORD_PROPERTY,
                        "value": {"recording": True},
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.record_start()

        assert client.put_calls == [(RECORD_PROPERTY, {"recording": True})]
        assert RECORD_PROPERTY not in client.calls  # no GET readback needed
        assert session.is_recording is True

    @pytest.mark.asyncio
    async def test_confirmed_by_get_readback_secondary_when_no_event_arrives(self):
        client = _storage_client(
            active=True,
            remaining_record_time=100,
            extra_responses={RECORD_PROPERTY: {"recording": True}},
        )
        session = make_session(make_profile_with_record_confirmed(), client=client)
        session.verify_timeout_s = 0.05

        await session.record_start()

        assert client.put_calls == [(RECORD_PROPERTY, {"recording": True})]
        assert RECORD_PROPERTY in client.calls

    @pytest.mark.asyncio
    async def test_raises_verification_error_when_neither_channel_confirms(self):
        client = _storage_client(
            active=True,
            remaining_record_time=100,
            extra_responses={RECORD_PROPERTY: {"recording": False}},
        )
        session = make_session(make_profile_with_record_confirmed(), client=client)
        session.verify_timeout_s = 0.05

        with pytest.raises(BMDVerificationError, match="record_start"):
            await session.record_start()


class TestRecordStop:
    @pytest.mark.asyncio
    async def test_noop_when_already_confirmed_stopped(self):
        session = make_session(make_profile(), client=FakeRestClient({}))
        session.is_recording = False

        await session.record_stop()  # must not raise despite no rest/ profile at all

    @pytest.mark.asyncio
    async def test_confirmed_by_ws_event_primary(self):
        client = FakeRestClient({})
        session = make_session(make_profile_with_record_confirmed(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": RECORD_PROPERTY,
                        "value": {"recording": False},
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.record_stop()

        assert client.put_calls == [(RECORD_PROPERTY, {"recording": False})]
        assert session.is_recording is False

    @pytest.mark.asyncio
    async def test_polls_get_readback_past_verify_timeout_s(self):
        """The scenario record_stop's widened budget exists for: the primary
        WS wait times out (no event arrives before verify_timeout_s), and
        the first secondary GET still reads the pre-stop value (the camera
        is mid-finalization) — real-hardware-confirmed shape,
        docs/rest/session.md 2026-08-05. A single-shot secondary would raise
        here; the poll must keep retrying until stop_verify_timeout_s."""
        client = FakeRestClient({RECORD_PROPERTY: {"recording": True}})
        session = make_session(make_profile_with_record_confirmed(), client=client)
        session.verify_timeout_s = 0.05
        session.stop_verify_timeout_s = 0.3

        async def flip_to_stopped_late():
            await asyncio.sleep(0.15)
            client.responses[RECORD_PROPERTY] = {"recording": False}

        asyncio.create_task(flip_to_stopped_late())
        await session.record_stop()

        assert client.calls.count(RECORD_PROPERTY) >= 2

    @pytest.mark.asyncio
    async def test_raises_verification_error_after_stop_verify_timeout_s(self):
        client = FakeRestClient({RECORD_PROPERTY: {"recording": True}})
        session = make_session(make_profile_with_record_confirmed(), client=client)
        session.verify_timeout_s = 0.02
        session.stop_verify_timeout_s = 0.06

        with pytest.raises(BMDVerificationError, match="record_stop"):
            await session.record_stop()

        assert client.calls.count(RECORD_PROPERTY) >= 2

    @pytest.mark.asyncio
    async def test_record_start_unaffected_by_stop_verify_timeout_s(self):
        """record_start's overall budget is verify_timeout_s regardless of
        stop_verify_timeout_s — a single secondary GET, exactly as before
        this change."""
        client = _storage_client(
            active=True,
            remaining_record_time=100,
            extra_responses={RECORD_PROPERTY: {"recording": False}},
        )
        session = make_session(make_profile_with_record_confirmed(), client=client)
        session.verify_timeout_s = 0.05
        session.stop_verify_timeout_s = 5.0

        with pytest.raises(BMDVerificationError, match="record_start"):
            await session.record_start()

        assert client.calls.count(RECORD_PROPERTY) == 1


class TestStopVerifyTimeoutDefault:
    def test_constructor_default_is_three_times_verify_timeout_s(self):
        session = RestCameraSession("cam.local", MODEL_KEY, FIRMWARE, verify_timeout_s=4.0)
        assert session.stop_verify_timeout_s == 12.0

    def test_constructor_honors_explicit_override(self):
        session = RestCameraSession(
            "cam.local", MODEL_KEY, FIRMWARE, verify_timeout_s=5.0, stop_verify_timeout_s=20.0
        )
        assert session.stop_verify_timeout_s == 20.0


def _clip(clip_unique_id: int) -> Clip:
    return Clip(
        clip_unique_id=clip_unique_id,
        file_path=f"/mnt/sd0/A001/clip_{clip_unique_id}.braw",
        codec="BRaw:5_1",
        container="BRAW",
        start_timecode="00:00:00:00",
        duration_timecode="00:00:10:00",
        video_format="4096x2160p23.98",
    )


def _clip_list_body(*clip_unique_ids: int) -> dict:
    return {
        "clipList": [
            {
                "clipUniqueId": cid,
                "filePath": f"/mnt/sd0/A001/clip_{cid}.braw",
                "codecFormat": {"codec": "BRaw:5_1", "container": "BRAW"},
                "startTimecode": "00:00:00:00",
                "durationTimecode": "00:00:10:00",
                "videoFormat": "4096x2160p23.98",
            }
            for cid in clip_unique_ids
        ]
    }


class TestConfirmNewClip:
    """Phase 9: formalizes the before/after clips() diff
    examples/rest_record_test_clip.py did by hand across three real
    real-hardware runs."""

    @pytest.mark.asyncio
    async def test_returns_the_one_new_clip(self):
        client = FakeRestClient({"/clips/list": _clip_list_body(1, 2)})
        session = make_session(make_profile(), client=client)

        result = await session.confirm_new_clip(clips_before=(_clip(1),))

        assert result.clip == _clip(2)
        assert result.bytes_written is None

    @pytest.mark.asyncio
    async def test_raises_when_no_new_clip_found(self):
        client = FakeRestClient({"/clips/list": _clip_list_body(1)})
        session = make_session(make_profile(), client=client)

        with pytest.raises(BMDVerificationError, match="no new clip"):
            await session.confirm_new_clip(clips_before=(_clip(1),))

    @pytest.mark.asyncio
    async def test_raises_when_multiple_new_clips_found(self):
        """Deliberately unguessed — see confirm_new_clip()'s own docstring
        for why this raises rather than picking one. No real-hardware run
        has ever produced this case; this is defensive coverage only."""
        client = FakeRestClient({"/clips/list": _clip_list_body(1, 2, 3)})
        session = make_session(make_profile(), client=client)

        with pytest.raises(BMDVerificationError, match="2 new clips"):
            await session.confirm_new_clip(clips_before=(_clip(1),))

    @pytest.mark.asyncio
    async def test_bytes_written_computed_from_storage_before_and_after(self):
        client = _storage_client(active=True, remaining_record_time=100, remaining_space=700)
        client.responses["/clips/list"] = _clip_list_body(1, 2)
        session = make_session(make_profile(), client=client)
        storage_before = StorageState(
            devices=(),
            active_device=StorageDevice(
                index=0, device_name="sd0", active=True, total_space=456, remaining_space=1000
            ),
        )

        result = await session.confirm_new_clip(
            clips_before=(_clip(1),), storage_before=storage_before
        )

        assert result.clip == _clip(2)
        assert result.bytes_written == 300

    @pytest.mark.asyncio
    async def test_bytes_written_is_none_when_storage_before_omitted(self):
        client = FakeRestClient({"/clips/list": _clip_list_body(1, 2)})
        session = make_session(make_profile(), client=client)

        result = await session.confirm_new_clip(clips_before=(_clip(1),))

        assert result.bytes_written is None

    @pytest.mark.asyncio
    async def test_bytes_written_is_none_when_storage_before_has_no_active_device(self):
        client = FakeRestClient({"/clips/list": _clip_list_body(1, 2)})
        session = make_session(make_profile(), client=client)
        storage_before = StorageState(devices=(), active_device=None)

        result = await session.confirm_new_clip(
            clips_before=(_clip(1),), storage_before=storage_before
        )

        assert result.bytes_written is None

    @pytest.mark.asyncio
    async def test_bytes_written_is_none_when_storage_after_has_no_active_device(self):
        client = _storage_client(active=False, remaining_record_time=0)
        client.responses["/clips/list"] = _clip_list_body(1, 2)
        session = make_session(make_profile(), client=client)
        storage_before = StorageState(
            devices=(),
            active_device=StorageDevice(
                index=0, device_name="sd0", active=True, total_space=456, remaining_space=1000
            ),
        )

        result = await session.confirm_new_clip(
            clips_before=(_clip(1),), storage_before=storage_before
        )

        assert result.bytes_written is None


EXPECTED_MERGED_BODY = {
    **CURRENT_FORMAT_BODY,
    "codec": "BRaw:5_1",
    "frameRate": "23.98",
    "recordResolution": {"width": 4096, "height": 2160},
    # BRaw's matched sensorResolution (4096x2160), NOT CURRENT_FORMAT_BODY's
    # stale ProRes-era value (5744x3024, see CURRENT_FORMAT_BODY above) —
    # the real-hardware defect TestSetCameraFormat's tests guard against.
    "sensorResolution": {"width": 4096, "height": 2160},
}


class TestSetCameraFormat:
    @pytest.mark.asyncio
    async def test_raises_value_error_for_unknown_codec(self):
        client = FakeRestClient({})
        session = make_session(make_format_profile(), client=client)

        with pytest.raises(ValueError, match="codec"):
            await session.set_camera_format("Nonexistent", "5:1", "4K DCI", "23.98")

        assert client.calls == []
        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_put_not_confirmed_in_profile(self):
        client = FakeRestClient({})
        profile = make_format_profile(format_endpoint_confirmed=False)
        session = make_session(profile, client=client)

        with pytest.raises(BMDUnsupportedError, match="system/format"):
            await session.set_camera_format("BRAW", "5:1", "4K DCI", "23.98")

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_supported_formats_endpoint_missing(self):
        client = FakeRestClient({})
        profile = make_format_profile(supported_formats_confirmed=False)
        session = make_session(profile, client=client)

        with pytest.raises(BMDUnsupportedError, match="supportedFormats"):
            await session.set_camera_format("BRAW", "5:1", "4K DCI", "23.98")

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_camera_does_not_report_combination(self):
        client = FakeRestClient(
            {
                "/system/supportedFormats": {
                    "supportedFormats": [
                        {
                            "codecs": ["ProRes:HQ"],
                            "frameRates": ["24"],
                            "recordResolution": {"width": 1920, "height": 1080},
                            "sensorResolution": {"width": 1920, "height": 1080},
                        }
                    ]
                }
            }
        )
        session = make_session(make_format_profile(), client=client)

        with pytest.raises(BMDUnsupportedError, match="does not report offering"):
            await session.set_camera_format("BRAW", "5:1", "4K DCI", "23.98")

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_confirmed_by_ws_event_primary(self):
        client = FakeRestClient(
            {
                FORMAT_PROPERTY: CURRENT_FORMAT_BODY,
                "/system/supportedFormats": SUPPORTED_FORMATS_BODY_BRAW_4K_DCI,
            }
        )
        session = make_session(make_format_profile(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": FORMAT_PROPERTY,
                        "value": {
                            "codec": "BRaw:5_1",
                            "frameRate": "23.98",
                            "recordResolution": {"width": 4096, "height": 2160},
                            "sensorResolution": {"width": 4096, "height": 2160},
                        },
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.set_camera_format("BRAW", "5:1", "4K DCI", "23.98")

        assert client.put_calls == [(FORMAT_PROPERTY, EXPECTED_MERGED_BODY)]
        # only one GET /system/format — the pre-write read; no readback needed
        assert client.calls.count(FORMAT_PROPERTY) == 1

    @pytest.mark.asyncio
    async def test_derives_sensor_resolution_from_matched_entry_not_current_format(self):
        """Real-hardware-confirmed defect (POCKET_6K_G2 v8.6, 2026-08-03):
        GET /system/supportedFormats pairs 4096x2160 recordResolution with
        DIFFERENT sensorResolution per codec (ProRes -> 5744x3024, BRaw ->
        4096x2160). A confirmed ProRes/4K DCI write followed by BRAW/4K DCI
        — CURRENT_FORMAT_BODY here stands in for that post-ProRes current
        format, sensorResolution 5744x3024 — must not carry that stale
        value into the BRAW write; it must use BRaw's own matched
        4096x2160, or the camera rejects the whole body with
        400 {"error": "Format is not supported"}."""
        client = FakeRestClient(
            {
                FORMAT_PROPERTY: CURRENT_FORMAT_BODY,
                "/system/supportedFormats": SUPPORTED_FORMATS_BODY_BRAW_4K_DCI,
            }
        )
        session = make_session(make_format_profile(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": FORMAT_PROPERTY,
                        "value": {
                            "codec": "BRaw:5_1",
                            "frameRate": "23.98",
                            "recordResolution": {"width": 4096, "height": 2160},
                            "sensorResolution": {"width": 4096, "height": 2160},
                        },
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.set_camera_format("BRAW", "5:1", "4K DCI", "23.98")

        _path, put_body = client.put_calls[0]
        assert put_body["sensorResolution"] == {"width": 4096, "height": 2160}
        assert put_body["sensorResolution"] != CURRENT_FORMAT_BODY["sensorResolution"]

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_matches_disagree_on_sensor_resolution(self):
        """If the camera's own capability matrix offers a (codec,
        recordResolution, fps) combination at more than one sensorResolution
        (e.g. ProRes at 1920x1080 — see docs/rest/transport.md), this method
        has no evidence for which one the caller wants and must not guess —
        guessing risks reproducing the exact "internally inconsistent body"
        failure the sensorResolution derivation exists to prevent."""
        client = FakeRestClient(
            {
                "/system/supportedFormats": {
                    "supportedFormats": [
                        {
                            "codecs": ["ProRes:HQ"],
                            "frameRates": ["24"],
                            "recordResolution": {"width": 1920, "height": 1080},
                            "sensorResolution": {"width": 2880, "height": 1512},
                        },
                        {
                            "codecs": ["ProRes:HQ"],
                            "frameRates": ["24"],
                            "recordResolution": {"width": 1920, "height": 1080},
                            "sensorResolution": {"width": 5376, "height": 3024},
                        },
                    ]
                }
            }
        )
        session = make_session(make_format_profile(), client=client)

        with pytest.raises(BMDUnsupportedError, match="different"):
            await session.set_camera_format("ProRes", "HQ", "HD", "24")

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_sensor_resolution_param_disambiguates_multiple_matches(self):
        """The explicit sensor_resolution override (tools/rest/sweep_camera_format.py's
        reason for existing) picks a specific pairing among several the
        camera offers, rather than refusing as ambiguous."""
        client = FakeRestClient(
            {
                FORMAT_PROPERTY: CURRENT_FORMAT_BODY,
                "/system/supportedFormats": {
                    "supportedFormats": [
                        {
                            "codecs": ["ProRes:HQ"],
                            "frameRates": ["24"],
                            "recordResolution": {"width": 1920, "height": 1080},
                            "sensorResolution": {"width": 2880, "height": 1512},
                        },
                        {
                            "codecs": ["ProRes:HQ"],
                            "frameRates": ["24"],
                            "recordResolution": {"width": 1920, "height": 1080},
                            "sensorResolution": {"width": 5376, "height": 3024},
                        },
                    ]
                },
            }
        )
        session = make_session(make_format_profile(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": FORMAT_PROPERTY,
                        "value": {
                            "codec": "ProRes:HQ",
                            "frameRate": "24",
                            "recordResolution": {"width": 1920, "height": 1080},
                            "sensorResolution": {"width": 5376, "height": 3024},
                        },
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.set_camera_format("ProRes", "HQ", "HD", "24", sensor_resolution=(5376, 3024))

        _path, put_body = client.put_calls[0]
        assert put_body["sensorResolution"] == {"width": 5376, "height": 3024}

    @pytest.mark.asyncio
    async def test_sensor_resolution_param_raises_when_camera_does_not_pair_it(self):
        client = FakeRestClient(
            {
                "/system/supportedFormats": {
                    "supportedFormats": [
                        {
                            "codecs": ["ProRes:HQ"],
                            "frameRates": ["24"],
                            "recordResolution": {"width": 1920, "height": 1080},
                            "sensorResolution": {"width": 2880, "height": 1512},
                        }
                    ]
                }
            }
        )
        session = make_session(make_format_profile(), client=client)

        with pytest.raises(BMDUnsupportedError, match="does not report offering"):
            await session.set_camera_format(
                "ProRes", "HQ", "HD", "24", sensor_resolution=(6144, 3456)
            )

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_confirmed_by_get_readback_secondary_when_no_event_arrives(self):
        confirmed_body = {**EXPECTED_MERGED_BODY}
        get_bodies = iter([CURRENT_FORMAT_BODY, confirmed_body])
        client = FakeRestClient({"/system/supportedFormats": SUPPORTED_FORMATS_BODY_BRAW_4K_DCI})

        async def get(path):
            client.calls.append(path)
            if path == FORMAT_PROPERTY:
                return next(get_bodies)
            return client.responses[path]

        client.get = get  # type: ignore[method-assign]
        session = make_session(make_format_profile(), client=client)
        session.verify_timeout_s = 0.05

        await session.set_camera_format("BRAW", "5:1", "4K DCI", "23.98")

        assert client.put_calls == [(FORMAT_PROPERTY, EXPECTED_MERGED_BODY)]
        assert client.calls.count(FORMAT_PROPERTY) == 2  # pre-write GET + readback

    @pytest.mark.asyncio
    async def test_raises_verification_error_when_neither_channel_confirms(self):
        client = FakeRestClient(
            {
                FORMAT_PROPERTY: CURRENT_FORMAT_BODY,  # readback never changes
                "/system/supportedFormats": SUPPORTED_FORMATS_BODY_BRAW_4K_DCI,
            }
        )
        session = make_session(make_format_profile(), client=client)
        session.verify_timeout_s = 0.05

        with pytest.raises(BMDVerificationError, match="set_camera_format"):
            await session.set_camera_format("BRAW", "5:1", "4K DCI", "23.98")

    @pytest.mark.asyncio
    async def test_uses_confirmed_format_names_over_derivation(self):
        """ProRes's '422' -> 'Original' is not derivable (mapping.py) — only
        a populated format_names entry gets this right; the derivation rule
        alone would (wrongly) produce 'ProRes:422'."""
        client = FakeRestClient(
            {
                FORMAT_PROPERTY: CURRENT_FORMAT_BODY,
                "/system/supportedFormats": {
                    "supportedFormats": [
                        {
                            "codecs": ["ProRes:Original"],
                            "frameRates": ["23.98"],
                            "recordResolution": {"width": 4096, "height": 2160},
                            "sensorResolution": {"width": 4096, "height": 2160},
                        }
                    ]
                },
            }
        )
        profile = make_format_profile(format_names={"ProRes": {"422": "ProRes:Original"}})
        session = make_session(profile, client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": FORMAT_PROPERTY,
                        "value": {
                            "codec": "ProRes:Original",
                            "frameRate": "23.98",
                            "recordResolution": {"width": 4096, "height": 2160},
                            "sensorResolution": {"width": 4096, "height": 2160},
                        },
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.set_camera_format("ProRes", "422", "4K DCI", "23.98")

        put_path, put_body = client.put_calls[0]
        assert put_body["codec"] == "ProRes:Original"


class TestWaitWhileRecording:
    """Mirrors CameraSession.wait_while_recording's return-value contract
    exactly: True = still recording (or state unknown) when the timeout
    elapses, False = a stop was confirmed before then. Real-hardware run
    (POCKET_6K_G2/POCKET_6K_PRO v8.6, 2026-08-03) caught the first version
    of this method returning the opposite of this contract — see
    wait_while_recording's docstring and docs/rest/session.md."""

    @pytest.mark.asyncio
    async def test_returns_false_immediately_when_already_stopped(self):
        session = make_session(make_profile())
        session.is_recording = False

        assert await session.wait_while_recording(timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_returns_true_after_timeout_when_unknown(self):
        session = make_session(make_profile())
        assert session.is_recording is None

        assert await session.wait_while_recording(timeout=0.05) is True

    @pytest.mark.asyncio
    async def test_returns_true_on_timeout_while_still_recording(self):
        session = make_session(make_profile())
        session.is_recording = True

        assert await session.wait_while_recording(timeout=0.05) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_stop_event_arrives_before_timeout(self):
        session = make_session(make_profile())
        session.is_recording = True

        async def stop_later():
            await asyncio.sleep(0.01)
            session._on_event("/transports/0/record", {"recording": False})

        asyncio.create_task(stop_later())

        assert await session.wait_while_recording(timeout=1.0) is False
        assert session.is_recording is False

    @pytest.mark.asyncio
    async def test_stale_stop_flag_from_earlier_cycle_does_not_leak_in(self):
        """A stop confirmed via the secondary GET-readback path (rather
        than the primary WS event) never touches _recording_stopped or
        is_recording (design principle 4 — see record_start/record_stop's
        docstrings), so a later cycle's wait_while_recording must not treat
        an earlier cycle's real stop-event flag as a fresh one. Simulates
        that by setting the flag directly (as an earlier real stop event
        would have) without going through _on_event, then confirming a
        call with no new event still waits out the full timeout."""
        session = make_session(make_profile())
        session.is_recording = True
        session._recording_stopped.set()  # stale, from a prior cycle

        assert await session.wait_while_recording(timeout=0.05) is True


class TestWaitForLowStorage:
    """Contract, stated explicitly given wait_while_recording's own history
    with an inverted-contract bug (see that class's docstring): True = low
    storage was observed (already true, or a pushed update crossed the
    threshold before timeout); False = timeout elapsed with storage still
    healthy. Opposite polarity from wait_while_recording's True on purpose
    — see wait_for_low_storage's own docstring."""

    @pytest.mark.asyncio
    async def test_raises_value_error_when_no_threshold_given(self):
        session = make_session(make_profile())

        with pytest.raises(ValueError, match="min_record_time_s"):
            await session.wait_for_low_storage(timeout=0.05)

    @pytest.mark.asyncio
    async def test_returns_true_immediately_when_already_low_on_record_time(self):
        session = make_session(make_profile())
        session._on_event(WORKINGSET_PROPERTY, REAL_WORKINGSET_EVENT)  # remaining=13107s

        assert await session.wait_for_low_storage(min_record_time_s=20000, timeout=0.05) is True

    @pytest.mark.asyncio
    async def test_returns_true_immediately_when_already_low_on_space(self):
        session = make_session(make_profile())
        session._on_event(WORKINGSET_PROPERTY, REAL_WORKINGSET_EVENT)  # remaining=943720932608

        assert await session.wait_for_low_storage(min_space_bytes=10**12, timeout=0.05) is True

    @pytest.mark.asyncio
    async def test_returns_true_immediately_when_no_active_device(self):
        session = make_session(make_profile())
        no_device_event = {"size": 1, "workingset": [{"activeDisk": False, "index": 0}]}
        session._on_event(WORKINGSET_PROPERTY, no_device_event)

        assert await session.wait_for_low_storage(min_space_bytes=1, timeout=0.05) is True

    @pytest.mark.asyncio
    async def test_returns_false_after_timeout_when_storage_stays_healthy(self):
        session = make_session(make_profile())
        session._on_event(WORKINGSET_PROPERTY, REAL_WORKINGSET_EVENT)

        assert await session.wait_for_low_storage(min_space_bytes=1, timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_returns_false_before_any_event_and_storage_stays_healthy(self):
        session = make_session(make_profile())
        assert session.last_known_storage is None

        assert await session.wait_for_low_storage(min_space_bytes=1, timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_returns_true_when_pushed_event_crosses_threshold_before_timeout(self):
        session = make_session(make_profile())
        session._on_event(WORKINGSET_PROPERTY, REAL_WORKINGSET_EVENT)  # healthy

        async def push_low_later():
            await asyncio.sleep(0.01)
            low_event = {
                "size": 3,
                "workingset": [
                    REAL_WORKINGSET_EVENT["workingset"][0],
                    {**REAL_WORKINGSET_EVENT["workingset"][1], "remainingSpace": 1},
                    REAL_WORKINGSET_EVENT["workingset"][2],
                ],
            }
            session._on_event(WORKINGSET_PROPERTY, low_event)

        asyncio.create_task(push_low_later())

        assert await session.wait_for_low_storage(min_space_bytes=1000, timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_threshold_cleared_after_call_does_not_leak_into_next_push(self):
        """A threshold armed by one call must not still be armed for an
        unrelated push after that call returns — mirrors
        wait_while_recording's own stale-flag discipline."""
        session = make_session(make_profile())
        session._on_event(WORKINGSET_PROPERTY, REAL_WORKINGSET_EVENT)

        await session.wait_for_low_storage(min_space_bytes=1, timeout=0.05)
        assert session._low_storage_min_space_bytes is None

        # A later push that would have crossed the old threshold must not
        # spuriously set an event nobody is waiting on.
        low_event = {
            "size": 3,
            "workingset": [
                REAL_WORKINGSET_EVENT["workingset"][0],
                {**REAL_WORKINGSET_EVENT["workingset"][1], "remainingSpace": 0},
                REAL_WORKINGSET_EVENT["workingset"][2],
            ],
        }
        session._on_event(WORKINGSET_PROPERTY, low_event)
        assert not session._low_storage_event.is_set()


# ── Connection lifecycle (real constructor, fake aiohttp session) ──────────


class FakeResponse:
    def __init__(self, status: int, json_body=None):
        self.status = status
        self._json_body = json_body
        self.content_length = 0 if json_body is None else 1

    async def json(self):
        if self._json_body is None:
            raise ValueError("no body")
        return self._json_body

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeWSMessage:
    def __init__(self, type_, data):
        self.type = type_
        self._data = data

    def json(self):
        return self._data


class FakeWebSocket:
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._queue.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)


class FakeAiohttpSession:
    """Combined fake supporting both RestClient's `.request()` (GET/PUT)
    and RestEventRouter's `.ws_connect()`."""

    def __init__(self, *, ws_connect_raises: Exception | None = None):
        self.responses: dict[str, FakeResponse] = {}
        self.ws = FakeWebSocket()
        self.requests: list[tuple[str, str]] = []
        self.closed = False
        self.ws_connect_raises = ws_connect_raises

    def request(self, method: str, url: str, *, json=None, timeout=None):
        self.requests.append((method, url))
        for path, response in self.responses.items():
            if url.endswith(path):
                return response
        raise AssertionError(f"No fake response configured for {url}")

    async def ws_connect(self, url: str, timeout=None):
        if self.ws_connect_raises is not None:
            raise self.ws_connect_raises
        return self.ws

    async def close(self) -> None:
        self.closed = True


class TestConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_aenter_subscribes_to_record_and_format_properties(self):
        """Real-hardware-confirmed defect this guards against
        (POCKET_6K_G2 v8.6, 2026-08-03, tools/rest/sweep_camera_format.py's
        first run): set_camera_format arms/waits on FORMAT_PROPERTY for its
        dual-check primary channel, but nothing ever subscribed the router
        to it — every one of 544 real writes in that run burned the full
        verify_timeout_s before falling through to the secondary GET
        readback. __aenter__ must subscribe to every property a write
        method later arms, not just RECORD_PROPERTY — extended for
        TRANSPORT_MODE_PROPERTY/PLAYBACK_PROPERTY (Phase 7) on the same
        principle, ahead of that phase's own first real-hardware run.
        WORKINGSET_PROPERTY (Phase 8 item 1) subscribed on the same
        principle, ahead of wait_for_low_storage()'s own first real-hardware
        run — confirmed live and pushing on real hardware first
        (POCKET_6K_G2 v8.6, 2026-08-05, tools/rest/watch_events.py) before
        this subscription was added, unlike the others above.
        PLAY_PROPERTY/STOP_PROPERTY (Phase 8 item 2) subscribed the same
        way — also confirmed live and pushing correctly-computed booleans
        on real hardware first (POCKET_6K_G2 v8.6, 2026-08-05,
        tools/rest/watch_events.py run alongside examples/rest_playback.py)
        before this subscription was added."""
        fake_session = FakeAiohttpSession()
        session = RestCameraSession("cam.local", MODEL_KEY, FIRMWARE, session=fake_session)

        async with session:
            pass

        assert fake_session.ws.sent == [
            {
                "type": "request",
                "id": 0,
                "data": {"action": "subscribe", "properties": [RECORD_PROPERTY]},
            },
            {
                "type": "request",
                "id": 0,
                "data": {"action": "subscribe", "properties": [FORMAT_PROPERTY]},
            },
            {
                "type": "request",
                "id": 0,
                "data": {"action": "subscribe", "properties": [TRANSPORT_MODE_PROPERTY]},
            },
            {
                "type": "request",
                "id": 0,
                "data": {"action": "subscribe", "properties": [PLAYBACK_PROPERTY]},
            },
            {
                "type": "request",
                "id": 0,
                "data": {"action": "subscribe", "properties": [WORKINGSET_PROPERTY]},
            },
            {
                "type": "request",
                "id": 0,
                "data": {"action": "subscribe", "properties": [PLAY_PROPERTY]},
            },
            {
                "type": "request",
                "id": 0,
                "data": {"action": "subscribe", "properties": [STOP_PROPERTY]},
            },
        ]

    @pytest.mark.asyncio
    async def test_injected_session_not_closed_on_exit(self):
        fake_session = FakeAiohttpSession()
        session = RestCameraSession("cam.local", MODEL_KEY, FIRMWARE, session=fake_session)

        async with session:
            pass

        assert fake_session.closed is False

    @pytest.mark.asyncio
    async def test_owned_session_closed_when_connect_fails(self, monkeypatch):
        """Real-hardware-confirmed leak (POCKET_6K_G2 v8.6, 2026-08-03):
        when the WS connect fails (e.g. the host doesn't resolve),
        __aenter__ never returns, so Python never calls __aexit__ — an
        aiohttp.ClientSession opened here must be closed on this failure
        path itself, or it leaks ("Unclosed client session"). No
        `session=` is injected here specifically so RestCameraSession
        creates (and must own the cleanup of) a real aiohttp.ClientSession,
        exactly like the real crash."""
        session = RestCameraSession("cam.local", MODEL_KEY, FIRMWARE)
        assert session._owns_session is True

        async def fail_connect(*args, **kwargs):
            raise BMDConnectionError("simulated DNS failure")

        monkeypatch.setattr(session._router, "connect", fail_connect)

        with pytest.raises(BMDConnectionError):
            async with session:
                pass

        assert session._session is None
        assert session._client is None

    @pytest.mark.asyncio
    async def test_injected_session_not_closed_when_connect_fails(self):
        """The failure-path cleanup must respect ownership exactly like the
        success path does — an injected session is never this session's to
        close, successful connect or not."""
        fake_session = FakeAiohttpSession(
            ws_connect_raises=aiohttp.ClientConnectionError("simulated DNS failure")
        )
        session = RestCameraSession("cam.local", MODEL_KEY, FIRMWARE, session=fake_session)
        assert session._owns_session is False

        with pytest.raises(BMDConnectionError):
            async with session:
                pass

        assert fake_session.closed is False

    @pytest.mark.asyncio
    async def test_events_delivered_after_connect_update_is_recording(self):
        fake_session = FakeAiohttpSession()
        session = RestCameraSession("cam.local", MODEL_KEY, FIRMWARE, session=fake_session)

        async with session:
            fake_session.ws._queue.put_nowait(
                FakeWSMessage(
                    aiohttp.WSMsgType.TEXT,
                    {
                        "type": "event",
                        "data": {
                            "action": "propertyValueChanged",
                            "property": "/transports/0/record",
                            "value": {"recording": True},
                        },
                    },
                )
            )
            await asyncio.sleep(0.05)
            assert session.is_recording is True


class TestEnterExitPlayback:
    """enter_playback()/exit_playback() -> _set_transport_mode(), the exact
    dual-check shape as _set_recording_state — see that class's own tests
    for the pattern this mirrors."""

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_endpoint_not_confirmed(self):
        session = make_session(make_profile(), client=FakeRestClient({}))  # no rest_raw

        with pytest.raises(BMDUnsupportedError, match="transports/0"):
            await session.enter_playback()

    @pytest.mark.asyncio
    async def test_enter_playback_confirmed_by_ws_event_primary(self):
        client = FakeRestClient({})
        session = make_session(make_playback_profile(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": TRANSPORT_MODE_PROPERTY,
                        "value": {"mode": "Output"},
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.enter_playback()

        assert client.put_calls == [(TRANSPORT_MODE_PROPERTY, {"mode": "Output"})]
        assert TRANSPORT_MODE_PROPERTY not in client.calls  # no GET readback needed

    @pytest.mark.asyncio
    async def test_exit_playback_confirmed_by_get_readback_secondary(self):
        client = FakeRestClient({TRANSPORT_MODE_PROPERTY: {"mode": "InputPreview"}})
        session = make_session(make_playback_profile(), client=client)
        session.verify_timeout_s = 0.05

        await session.exit_playback()

        assert client.put_calls == [(TRANSPORT_MODE_PROPERTY, {"mode": "InputPreview"})]
        assert TRANSPORT_MODE_PROPERTY in client.calls

    @pytest.mark.asyncio
    async def test_raises_verification_error_when_neither_channel_confirms(self):
        client = FakeRestClient({TRANSPORT_MODE_PROPERTY: {"mode": "InputRecord"}})
        session = make_session(make_playback_profile(), client=client)
        session.verify_timeout_s = 0.05

        with pytest.raises(BMDVerificationError, match="Output"):
            await session.enter_playback()

    @pytest.mark.asyncio
    async def test_enter_playback_sets_in_playback_and_clears_stale_interrupt(self):
        """Phase 8 item 2, part 2: _in_playback and playback_interrupted are
        set explicitly by enter_playback() itself, not left to _on_event —
        this run confirms via the secondary GET readback, which generates
        no WS event at all. Also resets _expected_speed to 0.0 (the camera
        opens playback paused), the baseline the speed-deviation interrupt
        check compares against."""
        client = FakeRestClient({TRANSPORT_MODE_PROPERTY: {"mode": "Output"}})
        session = make_session(make_playback_profile(), client=client)
        session.verify_timeout_s = 0.05
        session.playback_interrupted.set()  # stale flag from an earlier cycle
        session._expected_speed = 2.0  # stale value from an earlier cycle

        await session.enter_playback()

        assert session._in_playback is True
        assert not session.playback_interrupted.is_set()
        assert session._expected_speed == 0.0

    @pytest.mark.asyncio
    async def test_exit_playback_clears_in_playback(self):
        client = FakeRestClient({TRANSPORT_MODE_PROPERTY: {"mode": "InputPreview"}})
        session = make_session(make_playback_profile(), client=client)
        session.verify_timeout_s = 0.05
        session._in_playback = True

        await session.exit_playback()

        assert session._in_playback is False


class TestShuttleAndSeek:
    """shuttle()/seek() -> _put_playback(), a read-modify-write over the
    real confirmed body ({"type", "loop", "singleClip", "speed",
    "position"}, POCKET_6K_PRO v8.6, 2026-08-04) verified via the generic
    structural dual-check (_contains) against only the fields each call
    asked to change — see _put_playback's own docstring for why."""

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_endpoint_not_confirmed(self):
        session = make_session(make_profile(), client=FakeRestClient({}))

        with pytest.raises(BMDUnsupportedError, match="transports/0/playback"):
            await session.shuttle(1.0)

    @pytest.mark.asyncio
    async def test_shuttle_confirmed_by_ws_event_primary(self):
        client = FakeRestClient({PLAYBACK_PROPERTY: {"speed": 1.0}})
        session = make_session(make_playback_profile(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": PLAYBACK_PROPERTY,
                        "value": {"speed": 2.0},
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.shuttle(2.0)

        assert client.put_calls == [(PLAYBACK_PROPERTY, {"speed": 2.0})]
        # One GET — the initial read used to build the merged body — and no
        # second (secondary readback), since the WS event already confirmed.
        assert client.calls == [PLAYBACK_PROPERTY]

    @pytest.mark.asyncio
    async def test_shuttle_merges_speed_into_current_body(self):
        """The fix for the real 400 this endpoint's sibling (set_timeline)
        hit: never send a bare partial body. shuttle() must preserve
        type/loop/singleClip/position from the preceding GET, changing only
        speed. FakeRestClient's GET is a static canned value, so the
        secondary readback below would never itself reflect the merged
        write — confirmation comes via a WS event instead, same as
        test_shuttle_confirmed_by_ws_event_primary."""
        client = FakeRestClient(
            {
                PLAYBACK_PROPERTY: {
                    "type": "Play",
                    "loop": True,
                    "singleClip": False,
                    "speed": 1.0,
                    "position": 0,
                }
            }
        )
        session = make_session(make_playback_profile(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": PLAYBACK_PROPERTY,
                        "value": {"speed": 2.0},
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.shuttle(2.0)

        assert client.put_calls == [
            (
                PLAYBACK_PROPERTY,
                {
                    "type": "Play",
                    "loop": True,
                    "singleClip": False,
                    "speed": 2.0,
                    "position": 0,
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_shuttle_backward_confirmed_by_get_readback_secondary(self):
        client = FakeRestClient({PLAYBACK_PROPERTY: {"speed": -1.0}})
        session = make_session(make_playback_profile(), client=client)
        session.verify_timeout_s = 0.05

        await session.shuttle(-1.0)

        assert client.put_calls == [(PLAYBACK_PROPERTY, {"speed": -1.0})]

    @pytest.mark.asyncio
    async def test_seek_sends_position(self):
        client = FakeRestClient({PLAYBACK_PROPERTY: {"position": 12345}})
        session = make_session(make_playback_profile(), client=client)
        session.verify_timeout_s = 0.05

        await session.seek(12345)

        assert client.put_calls == [(PLAYBACK_PROPERTY, {"position": 12345})]

    @pytest.mark.asyncio
    async def test_seek_merges_position_into_current_body(self):
        client = FakeRestClient(
            {
                PLAYBACK_PROPERTY: {
                    "type": "Play",
                    "loop": False,
                    "singleClip": True,
                    "speed": 0.0,
                    "position": 0,
                }
            }
        )
        session = make_session(make_playback_profile(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": PLAYBACK_PROPERTY,
                        "value": {"position": 500},
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.seek(500)

        assert client.put_calls == [
            (
                PLAYBACK_PROPERTY,
                {"type": "Play", "loop": False, "singleClip": True, "speed": 0.0, "position": 500},
            )
        ]

    @pytest.mark.asyncio
    async def test_raises_verification_error_when_readback_missing_expected_fields(self):
        client = FakeRestClient({PLAYBACK_PROPERTY: {"speed": 0.0}})  # doesn't match 2.0
        session = make_session(make_playback_profile(), client=client)
        session.verify_timeout_s = 0.05

        with pytest.raises(BMDVerificationError, match="shuttle"):
            await session.shuttle(2.0)


class TestPlayPauseStop:
    """play()/pause()/stop() are thin aliases over shuttle()/exit_playback()
    — see their own docstrings for why they route through the
    write-confirmed /transports/0/playback and /transports/0 endpoints
    instead of the unswept dedicated /transports/0/play, /transports/0/stop
    triggers."""

    @pytest.mark.asyncio
    async def test_play_sends_speed_one(self):
        client = FakeRestClient({PLAYBACK_PROPERTY: {"speed": 1.0}})
        session = make_session(make_playback_profile(), client=client)
        session.verify_timeout_s = 0.05

        await session.play()

        assert client.put_calls == [(PLAYBACK_PROPERTY, {"speed": 1.0})]

    @pytest.mark.asyncio
    async def test_pause_sends_speed_zero(self):
        client = FakeRestClient({PLAYBACK_PROPERTY: {"speed": 0.0}})
        session = make_session(make_playback_profile(), client=client)
        session.verify_timeout_s = 0.05

        await session.pause()

        assert client.put_calls == [(PLAYBACK_PROPERTY, {"speed": 0.0})]

    @pytest.mark.asyncio
    async def test_stop_sets_transport_mode_to_input_preview(self):
        client = FakeRestClient({TRANSPORT_MODE_PROPERTY: {"mode": "InputPreview"}})
        session = make_session(make_playback_profile(), client=client)
        session.verify_timeout_s = 0.05

        await session.stop()

        assert client.put_calls == [(TRANSPORT_MODE_PROPERTY, {"mode": "InputPreview"})]


class TestPlaybackInterrupted:
    """playback_interrupted / wait_for_playback_interrupt() — Phase 8 item 2,
    part 2's camera-initiated playback interrupt detection. Notification-
    driven only (design principle 4): never set from a write this session
    itself made. See _on_event's docstring for the race-free argument the
    in-flight-guard tests below exercise directly.

    The speed trigger compares against _expected_speed — the speed value
    this session's own last confirmed write actually set — rather than a
    fixed speed == 0 check, so a camera-initiated deviation to *any* other
    speed counts as an interrupt, not only a full stop."""

    def test_speed_deviating_from_expected_sets_interrupted(self):
        session = make_session(make_profile())
        session._in_playback = True
        session._expected_speed = 1.0

        session._on_event(PLAYBACK_PROPERTY, {"speed": 0.0})

        assert session.playback_interrupted.is_set()

    def test_speed_deviating_to_nonzero_value_sets_interrupted(self):
        """The whole point of comparing against _expected_speed instead of
        a fixed 0: a camera-initiated speed change that lands somewhere
        other than 0 (e.g. 2.0 -> 1.0 on its own) is just as much
        "not what I asked for" as landing on 0."""
        session = make_session(make_profile())
        session._in_playback = True
        session._expected_speed = 2.0

        session._on_event(PLAYBACK_PROPERTY, {"speed": 1.0})

        assert session.playback_interrupted.is_set()

    def test_speed_matching_expected_does_not_set_interrupted(self):
        session = make_session(make_profile())
        session._in_playback = True
        session._expected_speed = 1.0

        session._on_event(PLAYBACK_PROPERTY, {"speed": 1.0})

        assert not session.playback_interrupted.is_set()

    def test_speed_event_ignored_when_expected_speed_unknown(self):
        """_expected_speed is None until enter_playback() sets it — with
        no baseline to compare against, no speed value can be judged an
        interrupt."""
        session = make_session(make_profile())
        session._in_playback = True
        session._expected_speed = None

        session._on_event(PLAYBACK_PROPERTY, {"speed": 0.0})

        assert not session.playback_interrupted.is_set()

    def test_speed_deviation_ignored_while_write_in_flight(self):
        """The in-flight guard _put_playback() holds for its own dual-check
        — a self-requested pause()/shuttle(0.0) must never be misread as a
        camera-initiated interrupt."""
        session = make_session(make_profile())
        session._in_playback = True
        session._expected_speed = 1.0
        session._playback_write_in_flight = True

        session._on_event(PLAYBACK_PROPERTY, {"speed": 0.0})

        assert not session.playback_interrupted.is_set()

    def test_speed_deviation_ignored_while_transport_mode_write_in_flight(self):
        """Regression test for a real-hardware-confirmed defect
        (POCKET_6K_G2 v8.6, 2026-08-05, tools/rest/verify_playback_interrupt.py's
        own sanity phase): leaving "Output" mode (_set_transport_mode, e.g.
        via stop()/exit_playback()) also pushes a side-effect PLAYBACK_PROPERTY
        event reporting speed=0 — the original guard only checked
        _playback_write_in_flight, which is False during a
        _set_transport_mode() write, so this side-effect push incorrectly
        set playback_interrupted on a purely self-requested stop()."""
        session = make_session(make_profile())
        session._in_playback = True
        session._expected_speed = 1.0
        session._transport_mode_write_in_flight = True

        session._on_event(PLAYBACK_PROPERTY, {"speed": 0.0})

        assert not session.playback_interrupted.is_set()

    def test_speed_deviation_ignored_when_not_in_playback(self):
        session = make_session(make_profile())
        session._in_playback = False
        session._expected_speed = 1.0

        session._on_event(PLAYBACK_PROPERTY, {"speed": 0.0})

        assert not session.playback_interrupted.is_set()

    def test_mode_leaving_output_sets_interrupted_and_clears_in_playback(self):
        session = make_session(make_profile())
        session._in_playback = True

        session._on_event(TRANSPORT_MODE_PROPERTY, {"mode": "InputPreview"})

        assert session.playback_interrupted.is_set()
        assert session._in_playback is False

    def test_mode_leaving_output_ignored_while_transport_mode_write_in_flight(self):
        """The in-flight guard _set_transport_mode() holds for its own
        dual-check — a self-requested exit_playback()/stop() must never be
        misread as a camera-initiated interrupt."""
        session = make_session(make_profile())
        session._in_playback = True
        session._transport_mode_write_in_flight = True

        session._on_event(TRANSPORT_MODE_PROPERTY, {"mode": "InputPreview"})

        assert not session.playback_interrupted.is_set()
        assert session._in_playback is True

    def test_mode_leaving_output_ignored_while_playback_write_in_flight(self):
        """Symmetric precaution to the regression above — no real-hardware
        evidence yet of a _put_playback() write causing a side-effect
        TRANSPORT_MODE_PROPERTY push, but checking both flags here too
        avoids assuming these two write paths are more independent than the
        one real-hardware run actually showed."""
        session = make_session(make_profile())
        session._in_playback = True
        session._playback_write_in_flight = True

        session._on_event(TRANSPORT_MODE_PROPERTY, {"mode": "InputPreview"})

        assert not session.playback_interrupted.is_set()
        assert session._in_playback is True

    def test_mode_still_output_does_not_set_interrupted(self):
        session = make_session(make_profile())
        session._in_playback = True

        session._on_event(TRANSPORT_MODE_PROPERTY, {"mode": "Output"})

        assert not session.playback_interrupted.is_set()
        assert session._in_playback is True

    def test_last_known_play_and_stop_track_events(self):
        session = make_session(make_profile())

        session._on_event(PLAY_PROPERTY, True)
        assert session.last_known_play is True

        session._on_event(STOP_PROPERTY, False)
        assert session.last_known_stop is False

    def test_play_stop_events_never_set_interrupted_directly(self):
        """last_known_play/last_known_stop are purely observational — only
        PLAYBACK_PROPERTY's own speed field and TRANSPORT_MODE_PROPERTY
        drive playback_interrupted (_on_event's docstring explains why:
        no ordering guarantee between these two independently-pushed
        properties and _put_playback()'s own in-flight guard)."""
        session = make_session(make_profile())
        session._in_playback = True

        session._on_event(STOP_PROPERTY, True)
        session._on_event(PLAY_PROPERTY, False)

        assert not session.playback_interrupted.is_set()

    @pytest.mark.asyncio
    async def test_pause_confirmed_by_ws_event_does_not_set_interrupted(self):
        """Integration-level race check: the exact WS delivery that
        confirms a self-requested pause() must not also flip
        playback_interrupted, even though the new speed (0.0) differs from
        the pre-pause _expected_speed (1.0) — the in-flight guard, not the
        "no baseline yet" case, has to be what prevents this."""
        client = FakeRestClient({PLAYBACK_PROPERTY: {"speed": 0.0}})
        session = make_session(make_playback_profile(), client=client)
        session._in_playback = True
        session._expected_speed = 1.0

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": PLAYBACK_PROPERTY,
                        "value": {"speed": 0.0},
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.pause()

        assert not session.playback_interrupted.is_set()
        assert session._expected_speed == 0.0  # _put_playback() updates it on success

    @pytest.mark.asyncio
    async def test_stop_confirmed_by_ws_event_does_not_set_interrupted(self):
        client = FakeRestClient({TRANSPORT_MODE_PROPERTY: {"mode": "InputPreview"}})
        session = make_session(make_playback_profile(), client=client)
        session._in_playback = True

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": TRANSPORT_MODE_PROPERTY,
                        "value": {"mode": "InputPreview"},
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.stop()

        assert not session.playback_interrupted.is_set()
        assert session._in_playback is False  # exit_playback() itself sets this

    @pytest.mark.asyncio
    async def test_stop_with_side_effect_playback_event_does_not_set_interrupted(self):
        """Reproduces the exact real-hardware sequence that exposed the
        original single-flag guard bug (POCKET_6K_G2 v8.6, 2026-08-05):
        stop()'s own TRANSPORT_MODE_PROPERTY confirmation arrives alongside
        a side-effect PLAYBACK_PROPERTY speed=0 push the camera sends when
        leaving "Output" mode. Both must be absorbed without setting
        playback_interrupted. _expected_speed is set to 1.0 (not 0.0) so
        the in-flight guard, not the "no baseline yet" case, is what's
        actually being exercised."""
        client = FakeRestClient({TRANSPORT_MODE_PROPERTY: {"mode": "InputPreview"}})
        session = make_session(make_playback_profile(), client=client)
        session._in_playback = True
        session._expected_speed = 1.0

        async def deliver_events():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": TRANSPORT_MODE_PROPERTY,
                        "value": {"mode": "InputPreview"},
                    },
                }
            )
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": PLAYBACK_PROPERTY,
                        "value": {"speed": 0.0},
                    },
                }
            )

        asyncio.create_task(deliver_events())
        await session.stop()
        await asyncio.sleep(0.02)  # let the side-effect event above land

        assert not session.playback_interrupted.is_set()
        assert session._in_playback is False


class TestWaitForPlaybackInterrupt:
    @pytest.mark.asyncio
    async def test_returns_true_immediately_when_already_set(self):
        session = make_session(make_profile())
        session.playback_interrupted.set()

        assert await session.wait_for_playback_interrupt(timeout=0.05) is True

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self):
        session = make_session(make_profile())

        assert await session.wait_for_playback_interrupt(timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_returns_true_when_set_before_timeout(self):
        session = make_session(make_profile())

        async def interrupt_soon():
            await asyncio.sleep(0.01)
            session.playback_interrupted.set()

        asyncio.create_task(interrupt_soon())

        assert await session.wait_for_playback_interrupt(timeout=1.0) is True


class TestTimelineClipIds:
    """timeline_clip_ids() — a plain GET /timelines/0, no format switch, no
    select_clip()-style poll. Added for examples/check_timeline_stale_entries.py,
    which needs to read the timeline independently of select_clip()'s own
    internal membership check."""

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_endpoint_not_confirmed(self):
        session = make_session(make_profile(), client=FakeRestClient({}))

        with pytest.raises(BMDUnsupportedError, match="timelines/0"):
            await session.timeline_clip_ids()

    @pytest.mark.asyncio
    async def test_returns_parsed_clip_ids(self):
        client = FakeRestClient(
            {
                TIMELINE_PATH: {
                    "clips": [
                        {"clipUniqueId": 10, "frameCount": 118},
                        {"clipUniqueId": 1, "frameCount": 60},
                    ]
                }
            }
        )
        session = make_session(make_playback_profile(), client=client)

        assert await session.timeline_clip_ids() == (10, 1)

    @pytest.mark.asyncio
    async def test_empty_timeline(self):
        client = FakeRestClient({TIMELINE_PATH: {"clips": []}})
        session = make_session(make_playback_profile(), client=client)

        assert await session.timeline_clip_ids() == ()


class TestSelectClip:
    """select_clip() replaces the old set_timeline(clip_unique_ids: list[int])
    — real hardware (POCKET_6K_G2/POCKET_6K_PRO v8.6, 2026-08-04) disproved
    its whole premise: the camera has no notion of a caller-curated
    playlist, and a POST's requested clipUniqueId doesn't select which
    clips end up in the timeline at all — it's always every clip matching
    the camera's *current* format. See select_clip()'s own docstring for
    the full four-round debugging trail."""

    @pytest.mark.asyncio
    async def test_raises_value_error_when_clip_not_found(self):
        client = FakeRestClient({"/clips/list": {"clipList": [CLIP_1_BODY]}})
        session = make_session(make_select_clip_profile(), client=client)

        with pytest.raises(ValueError, match="clip_unique_id=99"):
            await session.select_clip(99)

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_clip_missing_codec(self):
        clip_body = {**CLIP_1_BODY, "codecFormat": None}
        client = FakeRestClient({"/clips/list": {"clipList": [clip_body]}})
        session = make_session(make_select_clip_profile(), client=client)

        with pytest.raises(BMDUnsupportedError, match="codec/videoFormat"):
            await session.select_clip(1)

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_video_format_unparseable(self):
        clip_body = {**CLIP_1_BODY, "videoFormat": "not-a-real-format"}
        client = FakeRestClient({"/clips/list": {"clipList": [clip_body]}})
        session = make_session(make_select_clip_profile(), client=client)

        with pytest.raises(BMDUnsupportedError, match="videoFormat"):
            await session.select_clip(1)

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_codec_not_in_format_names(self):
        """Mismatched current format forces the reverse codec lookup;
        format_names is empty, so resolve_ble_codec_name has nothing to
        find clip 1's ProRes:Proxy in — no derivation fallback exists for
        the reverse direction (mapping.py's own docstring)."""
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [CLIP_1_BODY]},
                FORMAT_PROPERTY: CURRENT_FORMAT_BODY,
            }
        )
        session = make_session(make_select_clip_profile(format_names={}), client=client)

        with pytest.raises(BMDUnsupportedError, match="format_names"):
            await session.select_clip(1)

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_resolution_not_in_profile(self):
        """Mismatched current format forces the reverse resolution lookup;
        the profile's resolutions table has no 4096x2160 entry at all."""
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [CLIP_1_BODY]},
                FORMAT_PROPERTY: CURRENT_FORMAT_BODY,
            }
        )
        session = make_session(make_select_clip_profile(resolutions={}), client=client)

        with pytest.raises(BMDUnsupportedError, match="resolutions"):
            await session.select_clip(1)

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_timeline_endpoint_not_confirmed(self):
        """Format already matches (no set_camera_format call needed), so
        this exercises the TIMELINE_PATH capability check on its own."""
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [CLIP_1_BODY]},
                FORMAT_PROPERTY: MATCHING_FORMAT_BODY,
            }
        )
        session = make_session(make_select_clip_profile(timeline_confirmed=False), client=client)

        with pytest.raises(BMDUnsupportedError, match="timelines/0"):
            await session.select_clip(1)

    @pytest.mark.asyncio
    async def test_skips_format_switch_when_already_matching(self):
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [CLIP_1_BODY]},
                FORMAT_PROPERTY: MATCHING_FORMAT_BODY,
                TIMELINE_PATH: {"clips": [{"clipUniqueId": 1}]},
            }
        )
        session = make_session(make_select_clip_profile(), client=client)

        await session.select_clip(1)

        assert client.put_calls == []
        # Exactly one GET /system/format — select_clip's own comparison
        # read. set_camera_format's internal read never happens because
        # it's never called.
        assert client.calls.count(FORMAT_PROPERTY) == 1
        assert client.delete_calls == [TIMELINE_PATH]
        assert client.post_calls == [(TIMELINE_ADD_PATH, {"clips": [{"clipUniqueId": 1}]})]

    @pytest.mark.asyncio
    async def test_switches_format_before_syncing_timeline(self):
        """Current format is CURRENT_FORMAT_BODY's 1920x1080 — mismatched
        with clip 1's 4096x2160p24 — so select_clip must call
        set_camera_format("ProRes", "Proxy", "4K DCI", "24") before
        touching /timelines/0 at all."""
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [CLIP_1_BODY]},
                FORMAT_PROPERTY: CURRENT_FORMAT_BODY,
                "/system/supportedFormats": SUPPORTED_FORMATS_BODY_PRORES_PROXY_4K_DCI,
                TIMELINE_PATH: {"clips": [{"clipUniqueId": 1}]},
            }
        )
        session = make_session(make_select_clip_profile(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": FORMAT_PROPERTY,
                        "value": {
                            "codec": "ProRes:Proxy",
                            "frameRate": "24",
                            "recordResolution": {"width": 4096, "height": 2160},
                            "sensorResolution": {"width": 5744, "height": 3024},
                        },
                    },
                }
            )

        asyncio.create_task(deliver_event())
        await session.select_clip(1)

        expected_format_body = {
            **CURRENT_FORMAT_BODY,
            "codec": "ProRes:Proxy",
            "frameRate": "24",
            "recordResolution": {"width": 4096, "height": 2160},
            "sensorResolution": {"width": 5744, "height": 3024},
        }
        assert client.put_calls == [(FORMAT_PROPERTY, expected_format_body)]
        assert client.delete_calls == [TIMELINE_PATH]
        assert client.post_calls == [(TIMELINE_ADD_PATH, {"clips": [{"clipUniqueId": 1}]})]

    @pytest.mark.asyncio
    async def test_logs_format_mismatch_and_switch_at_info_level(self, caplog):
        """Real-world gap this closes: RestClient's own PUT/GET logging is
        DEBUG-only (client.py), so a format switch buried inside
        select_clip() was invisible at the INFO level examples/
        rest_playback.py's logging.basicConfig uses — an operator running
        the example against a genuinely mismatched clip saw playback
        succeed but no indication a PUT /system/format ever happened."""
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [CLIP_1_BODY]},
                FORMAT_PROPERTY: CURRENT_FORMAT_BODY,
                "/system/supportedFormats": SUPPORTED_FORMATS_BODY_PRORES_PROXY_4K_DCI,
                TIMELINE_PATH: {"clips": [{"clipUniqueId": 1}]},
            }
        )
        session = make_session(make_select_clip_profile(), client=client)

        async def deliver_event():
            await asyncio.sleep(0.01)
            session._router.handle_event(
                {
                    "type": "event",
                    "data": {
                        "action": "propertyValueChanged",
                        "property": FORMAT_PROPERTY,
                        "value": {
                            "codec": "ProRes:Proxy",
                            "frameRate": "24",
                            "recordResolution": {"width": 4096, "height": 2160},
                            "sensorResolution": {"width": 5744, "height": 3024},
                        },
                    },
                }
            )

        asyncio.create_task(deliver_event())
        with caplog.at_level(logging.INFO):
            await session.select_clip(1)

        messages = [record.message for record in caplog.records]
        assert any("does not match the camera's current format" in m for m in messages)
        assert any("Setting camera format" in m and "ProRes" in m for m in messages)

    @pytest.mark.asyncio
    async def test_no_format_switch_log_when_already_matching(self, caplog):
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [CLIP_1_BODY]},
                FORMAT_PROPERTY: MATCHING_FORMAT_BODY,
                TIMELINE_PATH: {"clips": [{"clipUniqueId": 1}]},
            }
        )
        session = make_session(make_select_clip_profile(), client=client)

        with caplog.at_level(logging.INFO):
            await session.select_clip(1)

        messages = [record.message for record in caplog.records]
        assert not any("does not match the camera's current format" in m for m in messages)
        assert not any("Setting camera format" in m for m in messages)

    @pytest.mark.asyncio
    async def test_readback_extra_fields_do_not_block_a_match(self):
        """Real GET /timelines/0 body, POCKET_6K_PRO v8.6, 2026-08-04 (operator
        Postman debugging): {"clips": [{"clipUniqueId": 12, "frameCount": 5020}]}
        — an extra "frameCount" field alongside "clipUniqueId" that
        _parse_timeline_clip_ids() must simply ignore. Also confirms
        membership (not exact-list equality): the real timeline holds six
        other clips too, not just the one requested."""
        clip_body = {**CLIP_1_BODY, "clipUniqueId": 12}
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [clip_body]},
                FORMAT_PROPERTY: {**MATCHING_FORMAT_BODY},
                TIMELINE_PATH: {
                    "clips": [
                        {"clipUniqueId": 10, "frameCount": 118},
                        {"clipUniqueId": 12, "frameCount": 5020},
                        {"clipUniqueId": 9, "frameCount": 119},
                    ]
                },
            }
        )
        session = make_session(make_select_clip_profile(), client=client)

        await session.select_clip(12)

    @pytest.mark.asyncio
    async def test_polls_until_readback_matches(self):
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [CLIP_1_BODY]},
                FORMAT_PROPERTY: MATCHING_FORMAT_BODY,
            }
        )
        session = make_session(make_select_clip_profile(), client=client)
        session.verify_timeout_s = 1.0

        bodies = iter(
            [
                {"clips": []},
                {"clips": [{"clipUniqueId": 1}]},
            ]
        )

        async def get(path, *, api_prefixed: bool = True):
            client.calls.append(path)
            if path == TIMELINE_PATH:
                return next(bodies)
            return client.responses[path]

        client.get = get

        await session.select_clip(1, poll_interval_s=0.01)

    @pytest.mark.asyncio
    async def test_raises_verification_error_on_timeout(self):
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [CLIP_1_BODY]},
                FORMAT_PROPERTY: MATCHING_FORMAT_BODY,
                TIMELINE_PATH: {"clips": []},
            }
        )
        session = make_session(make_select_clip_profile(), client=client)
        session.verify_timeout_s = 0.1

        with pytest.raises(BMDVerificationError, match="select_clip"):
            await session.select_clip(1)

    @pytest.mark.asyncio
    async def test_continues_to_post_when_delete_returns_501(self):
        """Real-hardware finding, POCKET_6K_G2 v8.6, 2026-08-04 (Phase 7's
        first run): DELETE /timelines/0 returns 501 on this firmware. A
        confirmed 501 from the DELETE specifically must not block the
        POST that follows."""
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [CLIP_1_BODY]},
                FORMAT_PROPERTY: MATCHING_FORMAT_BODY,
                TIMELINE_PATH: {"clips": [{"clipUniqueId": 1}]},
            }
        )

        async def delete(path):
            client.delete_calls.append(path)
            raise BMDUnsupportedError(f"[cam.local] DELETE {path} — not implemented (501)")

        client.delete = delete
        session = make_session(make_select_clip_profile(), client=client)

        await session.select_clip(1)

        assert client.delete_calls == [TIMELINE_PATH]
        assert client.post_calls == [
            (TIMELINE_ADD_PATH, {"clips": [{"clipUniqueId": 1}]}),
        ]

    @pytest.mark.asyncio
    async def test_other_delete_errors_still_propagate(self):
        client = FakeRestClient(
            {
                "/clips/list": {"clipList": [CLIP_1_BODY]},
                FORMAT_PROPERTY: MATCHING_FORMAT_BODY,
            }
        )

        async def delete(path):
            raise BMDRestError(f"[cam.local] DELETE {path} -> 500", status=500, body=None)

        client.delete = delete
        session = make_session(make_select_clip_profile(), client=client)

        with pytest.raises(BMDRestError):
            await session.select_clip(1)


DEVICE_NAME = "sd0"
DOFORMAT_PATH = f"/media/devices/{DEVICE_NAME}/doformat"
DEVICE_INFO_PATH = f"/media/devices/{DEVICE_NAME}"
DOFORMAT_FILESYSTEMS_PATH = "/media/devices/doformatSupportedFilesystems"


def make_format_device_profile() -> CameraProfile:
    """A profile whose rest/ file confirms `.../doformat`'s GET side only —
    `tools/rest/probe_endpoints.py`'s NEVER_WRITE list means the PUT side
    can never be sweep-confirmed for this endpoint at all, so
    `format_device()` deliberately gates on `supported` (GET), not
    `put_supported`, unlike every other write in this file — see
    `format_device()`'s own docstring."""
    rest_raw = {
        "_meta": {"model_key": MODEL_KEY, "firmware": FIRMWARE, "status": "UNVERIFIED"},
        "endpoints": {DOFORMAT_PATH: {"status": 200, "supported": True}},
    }
    return make_profile(rest_raw=rest_raw)


class TestDeviceInfo:
    @pytest.mark.asyncio
    async def test_parses_state(self):
        client = FakeRestClient({DEVICE_INFO_PATH: {"state": "Mounted"}})
        session = make_session(make_profile(), client=client)

        info = await session.device_info(DEVICE_NAME)

        assert info.state == "Mounted"

    @pytest.mark.asyncio
    async def test_defaults_state_to_none_string_when_missing(self):
        client = FakeRestClient({DEVICE_INFO_PATH: {}})
        session = make_session(make_profile(), client=client)

        info = await session.device_info(DEVICE_NAME)

        assert info.state == "None"


class TestDoformatSupportedFilesystems:
    @pytest.mark.asyncio
    async def test_returns_parsed_filesystem_list(self):
        client = FakeRestClient({DOFORMAT_FILESYSTEMS_PATH: ["ExFat", "HFS"]})
        session = make_session(make_profile(), client=client)

        assert await session.doformat_supported_filesystems() == ("ExFat", "HFS")

    @pytest.mark.asyncio
    async def test_returns_empty_tuple_when_body_is_not_a_list(self):
        client = FakeRestClient({DOFORMAT_FILESYSTEMS_PATH: {}})
        session = make_session(make_profile(), client=client)

        assert await session.doformat_supported_filesystems() == ()


class TestFormatDevice:
    """format_device() — GET a one-time key, PUT it back with
    filesystem/volume, then poll device_info() for completion. Per the
    official BMD REST spec (MediaControl.yaml), the only media-erasure
    capability the REST API exposes at all — see the method's own
    docstring for why verification here is structurally weaker (no WS
    event exists for this operation) than every other write in this file,
    and why the capability check gates on `supported` rather than
    `put_supported`."""

    @pytest.mark.asyncio
    async def test_raises_value_error_when_confirm_is_false(self):
        client = FakeRestClient({})
        session = make_session(make_format_device_profile(), client=client)

        with pytest.raises(ValueError, match="confirm=True"):
            await session.format_device(DEVICE_NAME, confirm=False, filesystem="ExFat")

        assert client.calls == []
        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_filesystem_is_a_required_keyword_argument(self):
        """Real-hardware-confirmed, POCKET_6K_G2 v8.6, 2026-08-13: the first
        version of this method treated `filesystem` as optional, matching
        MediaControl.yaml's own schema — the camera rejected the omitted-
        filesystem PUT with 400 {"error": "Field 'filesystem' missing from
        request body."}. filesystem has no default now; omitting it
        entirely is a TypeError before this method's own body ever runs."""
        session = make_session(make_format_device_profile(), client=FakeRestClient({}))

        with pytest.raises(TypeError):
            await session.format_device(DEVICE_NAME, confirm=True)  # type: ignore[call-arg]

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_endpoint_not_confirmed(self):
        session = make_session(make_profile(), client=FakeRestClient({}))

        with pytest.raises(BMDUnsupportedError, match="doformat"):
            await session.format_device(DEVICE_NAME, confirm=True, filesystem="ExFat")

    @pytest.mark.asyncio
    async def test_raises_bmd_unsupported_when_filesystem_not_offered(self):
        client = FakeRestClient({DOFORMAT_FILESYSTEMS_PATH: ["ExFat"]})
        session = make_session(make_format_device_profile(), client=client)

        with pytest.raises(BMDUnsupportedError, match="HFS"):
            await session.format_device(DEVICE_NAME, confirm=True, filesystem="HFS")

        assert DOFORMAT_PATH not in client.calls
        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_raises_bmd_verification_error_when_no_key_returned(self):
        client = FakeRestClient(
            {
                DOFORMAT_PATH: {"deviceName": DEVICE_NAME},
                DOFORMAT_FILESYSTEMS_PATH: ["ExFat"],
            }
        )
        session = make_session(make_format_device_profile(), client=client)

        with pytest.raises(BMDVerificationError, match="no format key"):
            await session.format_device(
                DEVICE_NAME, confirm=True, filesystem="ExFat", volume="A002"
            )

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_full_flow_success_with_filesystem_and_volume(self):
        client = FakeRestClient(
            {
                DOFORMAT_PATH: {"deviceName": DEVICE_NAME, "key": "abc123"},
                DOFORMAT_FILESYSTEMS_PATH: ["ExFat", "HFS"],
                DEVICE_INFO_PATH: {"state": "Mounted"},
            }
        )
        session = make_session(make_format_device_profile(), client=client)

        async def drive_state_transitions():
            await asyncio.sleep(0.02)
            client.responses[DEVICE_INFO_PATH] = {"state": "Formatting"}
            await asyncio.sleep(0.02)
            client.responses[DEVICE_INFO_PATH] = {"state": "Mounted"}

        asyncio.create_task(drive_state_transitions())
        await session.format_device(
            DEVICE_NAME,
            confirm=True,
            filesystem="HFS",
            volume="My disk",
            timeout=1.0,
            poll_interval_s=0.01,
        )

        assert client.put_calls == [
            (DOFORMAT_PATH, {"key": "abc123", "filesystem": "HFS", "volume": "My disk"})
        ]

    @pytest.mark.asyncio
    async def test_defaults_volume_from_storage_state_when_not_given(self):
        """Second real-hardware finding, POCKET_6K_G2 v8.6, 2026-08-13: the
        camera also rejects a PUT with no 'volume' field, once 'filesystem'
        is no longer the blocking field. Unlike filesystem, this codebase
        can read a device's current volume via storage_state() — used here
        as the default rather than guessing or omitting the field."""
        client = FakeRestClient(
            {
                DOFORMAT_PATH: {"deviceName": DEVICE_NAME, "key": "abc123"},
                DOFORMAT_FILESYSTEMS_PATH: ["ExFat"],
                DEVICE_INFO_PATH: {"state": "Formatting"},
                WORKINGSET_PROPERTY: {
                    "size": 1,
                    "workingset": [
                        {
                            "index": 0,
                            "deviceName": DEVICE_NAME,
                            "activeDisk": True,
                            "totalSpace": 456,
                            "remainingSpace": 123,
                            "remainingRecordTime": 100,
                            "clipCount": 20,
                            "volume": "A002",
                        }
                    ],
                },
                "/media/active": {"deviceName": DEVICE_NAME, "workingsetIndex": 0},
            }
        )
        session = make_session(make_format_device_profile(), client=client)

        async def finish_formatting():
            await asyncio.sleep(0.02)
            client.responses[DEVICE_INFO_PATH] = {"state": "Uninitialised"}

        asyncio.create_task(finish_formatting())
        await session.format_device(
            DEVICE_NAME, confirm=True, filesystem="ExFat", timeout=1.0, poll_interval_s=0.01
        )

        assert client.put_calls == [
            (DOFORMAT_PATH, {"key": "abc123", "filesystem": "ExFat", "volume": "A002"})
        ]

    @pytest.mark.asyncio
    async def test_raises_value_error_when_device_not_in_storage_and_volume_not_given(self):
        client = FakeRestClient(
            {
                DOFORMAT_PATH: {"deviceName": DEVICE_NAME, "key": "abc123"},
                DOFORMAT_FILESYSTEMS_PATH: ["ExFat"],
                WORKINGSET_PROPERTY: {"size": 1, "workingset": [{"index": 0, "deviceName": ""}]},
                "/media/active": {"deviceName": "", "workingsetIndex": -1},
            }
        )
        session = make_session(make_format_device_profile(), client=client)

        with pytest.raises(ValueError, match="volume"):
            await session.format_device(DEVICE_NAME, confirm=True, filesystem="ExFat")

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_raises_value_error_when_matching_device_has_no_volume(self):
        client = FakeRestClient(
            {
                DOFORMAT_PATH: {"deviceName": DEVICE_NAME, "key": "abc123"},
                DOFORMAT_FILESYSTEMS_PATH: ["ExFat"],
                WORKINGSET_PROPERTY: {
                    "size": 1,
                    "workingset": [
                        {
                            "index": 0,
                            "deviceName": DEVICE_NAME,
                            "activeDisk": True,
                            "totalSpace": 456,
                        }
                    ],
                },
                "/media/active": {"deviceName": DEVICE_NAME, "workingsetIndex": 0},
            }
        )
        session = make_session(make_format_device_profile(), client=client)

        with pytest.raises(ValueError, match="volume"):
            await session.format_device(DEVICE_NAME, confirm=True, filesystem="ExFat")

        assert client.put_calls == []

    @pytest.mark.asyncio
    async def test_raises_verification_error_on_timeout_without_formatting_observed(self):
        client = FakeRestClient(
            {
                DOFORMAT_PATH: {"deviceName": DEVICE_NAME, "key": "abc123"},
                DOFORMAT_FILESYSTEMS_PATH: ["ExFat"],
                DEVICE_INFO_PATH: {"state": "Mounted"},
            }
        )
        session = make_session(make_format_device_profile(), client=client)

        with pytest.raises(BMDVerificationError, match="format_device"):
            await session.format_device(
                DEVICE_NAME,
                confirm=True,
                filesystem="ExFat",
                volume="A002",
                timeout=0.05,
                poll_interval_s=0.01,
            )


DELETE_CLIP_MOUNT_NAME = "A001-sd1"
DELETE_CLIP_TARGET = f"/mounts/{DELETE_CLIP_MOUNT_NAME}/clip_1.braw"


def _delete_clip_client(*, exists_before: bool = True) -> FakeRestClient:
    """Everything delete_clip() needs: clips() (one clip, clip_unique_id=1,
    file_path basename clip_1.braw), storage_state() + mount_names() (so
    resolve_active_mount() resolves unambiguously to the single mount
    A001-sd1), and an exists_responses entry for the real target path
    that composition resolves to."""
    return FakeRestClient(
        {
            "/clips/list": _clip_list_body(1),
            "/media/workingset": {
                "size": 1,
                "workingset": [
                    {
                        "activeDisk": True,
                        "clipCount": 1,
                        "deviceName": "sd0",
                        "index": 0,
                        "remainingRecordTime": 100,
                        "remainingSpace": 123,
                        "totalSpace": 456,
                        "volume": "A001",
                    }
                ],
            },
            "/media/active": {"deviceName": "sd0", "workingsetIndex": 0},
            "/mounts/": [{"name": DELETE_CLIP_MOUNT_NAME, "type": "directory"}],
        },
        exists_responses={DELETE_CLIP_TARGET: exists_before},
    )


class TestDeleteClip:
    """delete_clip() — real-hardware-confirmed working via Postman,
    POCKET_6K_G2 v8.6, 2026-08-13 (docs/rest/transport.md's Mode 3
    section): GET 200 -> DELETE 200 OK -> GET 404. This method composes
    that confirmed sequence through clips()/resolve_active_mount()/
    RestClient.exists() — not yet itself run against real hardware."""

    @pytest.mark.asyncio
    async def test_raises_value_error_when_confirm_is_false(self):
        client = _delete_clip_client()
        session = make_session(make_profile(), client=client)

        with pytest.raises(ValueError, match="confirm=True"):
            await session.delete_clip(1, confirm=False)

        assert client.calls == []
        assert client.delete_calls == []

    @pytest.mark.asyncio
    async def test_raises_value_error_when_clip_not_found(self):
        client = _delete_clip_client()
        session = make_session(make_profile(), client=client)

        with pytest.raises(ValueError, match="clip_unique_id=99"):
            await session.delete_clip(99, confirm=True)

        assert client.delete_calls == []

    @pytest.mark.asyncio
    async def test_raises_verification_error_when_resolved_path_not_found(self):
        client = _delete_clip_client(exists_before=False)
        session = make_session(make_profile(), client=client)

        with pytest.raises(BMDVerificationError, match="does not exist"):
            await session.delete_clip(1, confirm=True)

        assert client.delete_calls == []

    @pytest.mark.asyncio
    async def test_raises_verification_error_when_still_exists_after_delete(self):
        """delete() succeeding (no exception) is not itself proof of
        deletion — only a fresh exists() read counts, matching
        format_device()'s "confirm via a fresh read" discipline."""
        client = _delete_clip_client(exists_before=True)  # exists() never flips to False
        session = make_session(make_profile(), client=client)

        with pytest.raises(BMDVerificationError, match="still exists after DELETE"):
            await session.delete_clip(1, confirm=True)

        assert client.delete_calls == [DELETE_CLIP_TARGET]

    @pytest.mark.asyncio
    async def test_full_flow_success_returns_the_deleted_clip(self):
        client = _delete_clip_client(exists_before=True)
        call_count = {"n": 0}

        async def exists(path: str, *, api_prefixed: bool = True) -> bool:
            client.exists_calls.append(path)
            client.api_prefixed_calls[path] = api_prefixed
            call_count["n"] += 1
            return call_count["n"] == 1  # True before DELETE, False after

        client.exists = exists
        session = make_session(make_profile(), client=client)

        deleted = await session.delete_clip(1, confirm=True)

        assert deleted.clip_unique_id == 1
        assert client.delete_calls == [DELETE_CLIP_TARGET]
        assert client.api_prefixed_calls[DELETE_CLIP_TARGET] is False

    @pytest.mark.asyncio
    async def test_logs_warning_when_clip_still_listed_after_deletion(self, caplog):
        """Real-hardware finding, POCKET_6K_G2 v8.6, 2026-08-13
        (examples/rest_delete_clip.py): the file-level deletion was
        confirmed exactly as designed, but GET /clips/list still reported
        the clip immediately afterward in the same session. Informational
        only — must never raise, must never change the return value."""
        client = _delete_clip_client(exists_before=True)
        call_count = {"n": 0}

        async def exists(path: str, *, api_prefixed: bool = True) -> bool:
            call_count["n"] += 1
            return call_count["n"] == 1  # True before DELETE, False after

        client.exists = exists
        session = make_session(make_profile(), client=client)

        with caplog.at_level(logging.WARNING):
            deleted = await session.delete_clip(1, confirm=True)

        assert deleted.clip_unique_id == 1  # success is unaffected
        messages = [record.message for record in caplog.records]
        assert any("still appears in GET /clips/list" in m for m in messages)

    @pytest.mark.asyncio
    async def test_no_warning_when_clip_no_longer_listed_after_deletion(self, caplog):
        client = _delete_clip_client(exists_before=True)
        exists_call_count = {"n": 0}

        async def exists(path: str, *, api_prefixed: bool = True) -> bool:
            exists_call_count["n"] += 1
            return exists_call_count["n"] == 1

        client.exists = exists

        clips_call_count = {"n": 0}
        original_get = client.get

        async def get(path: str, *, api_prefixed: bool = True):
            if path == "/clips/list":
                clips_call_count["n"] += 1
                if clips_call_count["n"] >= 2:
                    return {"clipList": []}
            return await original_get(path, api_prefixed=api_prefixed)

        client.get = get
        session = make_session(make_profile(), client=client)

        with caplog.at_level(logging.WARNING):
            await session.delete_clip(1, confirm=True)

        messages = [record.message for record in caplog.records]
        assert not any("still appears in GET /clips/list" in m for m in messages)

    @pytest.mark.asyncio
    async def test_staleness_check_swallows_bmd_storage_error(self):
        """The post-deletion clips() check is best-effort (design principle
        9) — a BMDStorageError from it (e.g. the card reporting no media
        the instant after its only clip was removed) must not surface as
        a failure of an otherwise-confirmed deletion."""
        client = _delete_clip_client(exists_before=True)
        exists_call_count = {"n": 0}

        async def exists(path: str, *, api_prefixed: bool = True) -> bool:
            exists_call_count["n"] += 1
            return exists_call_count["n"] == 1

        client.exists = exists

        clips_call_count = {"n": 0}
        original_get = client.get

        async def get(path: str, *, api_prefixed: bool = True):
            if path == "/clips/list":
                clips_call_count["n"] += 1
                if clips_call_count["n"] >= 2:
                    raise BMDRestError("[cam.local] GET /clips/list -> 404", status=404, body=None)
            return await original_get(path, api_prefixed=api_prefixed)

        client.get = get
        session = make_session(make_profile(), client=client)

        deleted = await session.delete_clip(1, confirm=True)  # must not raise

        assert deleted.clip_unique_id == 1


DELETE_STILL_PATH = "/mounts/A002-sd1/Stills/A002_08120219_S001.braw"


class TestDeleteStill:
    """delete_still() — real-hardware-confirmed working via Postman,
    POCKET_6K_G2 v8.6, 2026-08-13: GET 200 -> DELETE 200 OK -> GET 404 on
    a real still. Unlike delete_clip(), the caller supplies the full
    /mounts/... path directly — there is no still-id/listing system to
    resolve one from."""

    @pytest.mark.asyncio
    async def test_raises_value_error_when_confirm_is_false(self):
        client = FakeRestClient({})
        session = make_session(make_profile(), client=client)

        with pytest.raises(ValueError, match="confirm=True"):
            await session.delete_still(DELETE_STILL_PATH, confirm=False)

        assert client.calls == []
        assert client.exists_calls == []
        assert client.delete_calls == []

    @pytest.mark.asyncio
    async def test_raises_verification_error_when_path_not_found(self):
        client = FakeRestClient({}, exists_responses={DELETE_STILL_PATH: False})
        session = make_session(make_profile(), client=client)

        with pytest.raises(BMDVerificationError, match="does not exist"):
            await session.delete_still(DELETE_STILL_PATH, confirm=True)

        assert client.delete_calls == []

    @pytest.mark.asyncio
    async def test_raises_verification_error_when_still_exists_after_delete(self):
        client = FakeRestClient({}, exists_responses={DELETE_STILL_PATH: True})
        session = make_session(make_profile(), client=client)
        session.verify_timeout_s = 0.05

        with pytest.raises(BMDVerificationError, match="still exists after DELETE"):
            await session.delete_still(DELETE_STILL_PATH, confirm=True, poll_interval_s=0.01)

        assert client.delete_calls == [DELETE_STILL_PATH]

    @pytest.mark.asyncio
    async def test_full_flow_success(self, caplog):
        client = FakeRestClient({})
        call_count = {"n": 0}

        async def exists(path: str, *, api_prefixed: bool = True) -> bool:
            client.exists_calls.append(path)
            client.api_prefixed_calls[path] = api_prefixed
            call_count["n"] += 1
            return call_count["n"] == 1  # True before DELETE, False after

        client.exists = exists
        session = make_session(make_profile(), client=client)

        with caplog.at_level(logging.INFO):
            await session.delete_still(DELETE_STILL_PATH, confirm=True)  # must not raise

        assert client.delete_calls == [DELETE_STILL_PATH]
        assert client.api_prefixed_calls[DELETE_STILL_PATH] is False
        # Regression test: an earlier edit inserting download_clip()/download_still()
        # right after delete_still() accidentally orphaned this success log line
        # past a `return`, so delete_still() silently stopped logging its own
        # confirmation. Guards against that specific class of copy-paste defect.
        messages = [record.message for record in caplog.records]
        assert any("deleted and confirmed gone" in m for m in messages)

    @pytest.mark.asyncio
    async def test_after_check_polls_past_a_transient_stale_exists(self):
        """Real-hardware finding, POCKET_6K_G2 v8.6, 2026-08-13: a single
        immediate exists() right after DELETE reported the file still
        present even though the deletion had genuinely succeeded (confirmed
        independently via the card's own contents). The after-check must
        retry rather than fail on the first stale read."""
        client = FakeRestClient({})
        call_count = {"n": 0}

        async def exists(path: str, *, api_prefixed: bool = True) -> bool:
            call_count["n"] += 1
            # True before DELETE, then one stale True, then finally False.
            return call_count["n"] in (1, 2)

        client.exists = exists
        session = make_session(make_profile(), client=client)

        await session.delete_still(
            DELETE_STILL_PATH, confirm=True, poll_interval_s=0.01
        )  # must not raise

        assert call_count["n"] == 3
        assert client.delete_calls == [DELETE_STILL_PATH]


DOWNLOAD_CLIP_MOUNT_NAME = "A001-sd1"
DOWNLOAD_CLIP_TARGET = f"/mounts/{DOWNLOAD_CLIP_MOUNT_NAME}/clip_1.braw"
DOWNLOAD_CLIP_CONTENT = b"fake clip bytes"


def _download_clip_client(*, content: bytes = DOWNLOAD_CLIP_CONTENT) -> FakeRestClient:
    """Everything download_clip() needs: clips() (one clip,
    clip_unique_id=1, file_path basename clip_1.braw), storage_state() +
    mount_names() (so resolve_active_mount() resolves unambiguously to the
    single mount A001-sd1), and a download_responses entry for the real
    target path that composition resolves to — mirrors
    _delete_clip_client() exactly, swapping exists_responses for
    download_responses."""
    return FakeRestClient(
        {
            "/clips/list": _clip_list_body(1),
            "/media/workingset": {
                "size": 1,
                "workingset": [
                    {
                        "activeDisk": True,
                        "clipCount": 1,
                        "deviceName": "sd0",
                        "index": 0,
                        "remainingRecordTime": 100,
                        "remainingSpace": 123,
                        "totalSpace": 456,
                        "volume": "A001",
                    }
                ],
            },
            "/media/active": {"deviceName": "sd0", "workingsetIndex": 0},
            "/mounts/": [{"name": DOWNLOAD_CLIP_MOUNT_NAME, "type": "directory"}],
        },
        download_responses={DOWNLOAD_CLIP_TARGET: content},
    )


class TestDownloadClip:
    """download_clip() — the mirror-image operation of delete_clip(),
    reusing the same clip-resolution and mount-path-construction logic.
    Not yet real-hardware-run; unit-tested against a fake client only."""

    @pytest.mark.asyncio
    async def test_raises_value_error_when_clip_not_found(self, tmp_path):
        client = FakeRestClient({"/clips/list": _clip_list_body(2)})
        session = make_session(make_profile(), client=client)

        with pytest.raises(ValueError, match="No clip with clip_unique_id=1"):
            await session.download_clip(1, tmp_path)

        assert client.download_calls == []

    @pytest.mark.asyncio
    async def test_raises_value_error_when_dest_dir_missing(self, tmp_path):
        client = _download_clip_client()
        session = make_session(make_profile(), client=client)

        with pytest.raises(ValueError, match="does not exist"):
            await session.download_clip(1, tmp_path / "nonexistent")

        assert client.download_calls == []

    @pytest.mark.asyncio
    async def test_raises_file_exists_error_without_overwrite(self, tmp_path):
        client = _download_clip_client()
        session = make_session(make_profile(), client=client)
        (tmp_path / "clip_1.braw").write_bytes(b"already here")

        with pytest.raises(FileExistsError, match="overwrite=True"):
            await session.download_clip(1, tmp_path)

        assert client.download_calls == []

    @pytest.mark.asyncio
    async def test_overwrite_true_replaces_existing_file(self, tmp_path):
        client = _download_clip_client()
        session = make_session(make_profile(), client=client)
        (tmp_path / "clip_1.braw").write_bytes(b"stale")

        dest = await session.download_clip(1, tmp_path, overwrite=True)

        assert dest == tmp_path / "clip_1.braw"
        assert dest.read_bytes() == DOWNLOAD_CLIP_CONTENT

    @pytest.mark.asyncio
    async def test_full_flow_success(self, tmp_path):
        client = _download_clip_client()
        session = make_session(make_profile(), client=client)

        dest = await session.download_clip(1, tmp_path)

        assert dest == tmp_path / "clip_1.braw"
        assert dest.read_bytes() == DOWNLOAD_CLIP_CONTENT
        assert client.download_calls == [(DOWNLOAD_CLIP_TARGET, str(dest))]
        assert client.api_prefixed_calls[DOWNLOAD_CLIP_TARGET] is False


DOWNLOAD_STILL_PATH = "/mounts/A002-sd1/Stills/A002_08120219_S001.braw"
DOWNLOAD_STILL_CONTENT = b"fake still bytes"


class TestDownloadStill:
    """download_still() — the mirror-image operation of delete_still():
    takes the full /mounts/... path directly, no listing to resolve
    against. Not yet real-hardware-run; unit-tested against a fake client
    only."""

    @pytest.mark.asyncio
    async def test_raises_value_error_when_dest_dir_missing(self, tmp_path):
        client = FakeRestClient({})
        session = make_session(make_profile(), client=client)

        with pytest.raises(ValueError, match="does not exist"):
            await session.download_still(DOWNLOAD_STILL_PATH, tmp_path / "nonexistent")

        assert client.download_calls == []

    @pytest.mark.asyncio
    async def test_raises_file_exists_error_without_overwrite(self, tmp_path):
        client = FakeRestClient({})
        session = make_session(make_profile(), client=client)
        (tmp_path / "A002_08120219_S001.braw").write_bytes(b"already here")

        with pytest.raises(FileExistsError, match="overwrite=True"):
            await session.download_still(DOWNLOAD_STILL_PATH, tmp_path)

        assert client.download_calls == []

    @pytest.mark.asyncio
    async def test_overwrite_true_replaces_existing_file(self, tmp_path):
        client = FakeRestClient(
            {}, download_responses={DOWNLOAD_STILL_PATH: DOWNLOAD_STILL_CONTENT}
        )
        session = make_session(make_profile(), client=client)
        (tmp_path / "A002_08120219_S001.braw").write_bytes(b"stale")

        dest = await session.download_still(DOWNLOAD_STILL_PATH, tmp_path, overwrite=True)

        assert dest == tmp_path / "A002_08120219_S001.braw"
        assert dest.read_bytes() == DOWNLOAD_STILL_CONTENT

    @pytest.mark.asyncio
    async def test_full_flow_success(self, tmp_path):
        client = FakeRestClient(
            {}, download_responses={DOWNLOAD_STILL_PATH: DOWNLOAD_STILL_CONTENT}
        )
        session = make_session(make_profile(), client=client)

        dest = await session.download_still(DOWNLOAD_STILL_PATH, tmp_path)

        assert dest == tmp_path / "A002_08120219_S001.braw"
        assert dest.read_bytes() == DOWNLOAD_STILL_CONTENT
        assert client.download_calls == [(DOWNLOAD_STILL_PATH, str(dest))]
        assert client.api_prefixed_calls[DOWNLOAD_STILL_PATH] is False


BULK_DELETE_MOUNT_NAME = "A001-sd1"


def _bulk_delete_client(*clip_unique_ids: int, exists_before: dict[int, bool] | None = None):
    """Everything delete_clips() needs for a multi-clip batch — clips()
    reporting every requested id, storage_state()/mount_names() so
    resolve_active_mount() resolves unambiguously, and a per-path
    exists() that toggles True (before DELETE) -> False (after), the same
    "confirmed deleted" shape TestDeleteClip's own full-flow tests use,
    applied independently per clip's target path. `exists_before[cid] =
    False` simulates that clip's file never having existed at all (stays
    False on every call), for testing a single clip's pre-check failure
    without affecting the others in the same batch."""
    exists_before = exists_before or {}
    call_counts: dict[str, int] = {}

    async def exists(path: str, *, api_prefixed: bool = True) -> bool:
        call_counts[path] = call_counts.get(path, 0) + 1
        cid = int(path.rsplit("_", 1)[-1].removesuffix(".braw"))
        if not exists_before.get(cid, True):
            return False
        return call_counts[path] == 1  # True before DELETE, False after

    client = FakeRestClient(
        {
            "/clips/list": _clip_list_body(*clip_unique_ids),
            "/media/workingset": {
                "size": 1,
                "workingset": [
                    {
                        "activeDisk": True,
                        "clipCount": len(clip_unique_ids),
                        "deviceName": "sd0",
                        "index": 0,
                        "remainingRecordTime": 100,
                        "remainingSpace": 123,
                        "totalSpace": 456,
                        "volume": "A001",
                    }
                ],
            },
            "/media/active": {"deviceName": "sd0", "workingsetIndex": 0},
            "/mounts/": [{"name": BULK_DELETE_MOUNT_NAME, "type": "directory"}],
        },
    )
    client.exists = exists
    return client


class TestDeleteClips:
    """delete_clips() — built entirely on delete_clip(), called once per
    id. Not yet real-hardware-run; unit-tested against a fake client only."""

    @pytest.mark.asyncio
    async def test_raises_value_error_when_confirm_is_false(self):
        client = _bulk_delete_client(1, 2)
        session = make_session(make_profile(), client=client)

        with pytest.raises(ValueError, match="confirm=True"):
            await session.delete_clips([1, 2], confirm=False)

        assert client.calls == []
        assert client.delete_calls == []

    @pytest.mark.asyncio
    async def test_raises_value_error_when_no_ids(self):
        client = _bulk_delete_client(1, 2)
        session = make_session(make_profile(), client=client)

        with pytest.raises(ValueError, match="no clip_unique_ids"):
            await session.delete_clips([], confirm=True)

        assert client.delete_calls == []

    @pytest.mark.asyncio
    async def test_deduplicates_ids(self):
        client = _bulk_delete_client(1, 2)
        session = make_session(make_profile(), client=client)

        result = await session.delete_clips([1, 1, 2], confirm=True)

        assert len(result.deleted) == 2
        assert {c.clip_unique_id for c in result.deleted} == {1, 2}
        assert client.delete_calls == [
            f"/mounts/{BULK_DELETE_MOUNT_NAME}/clip_1.braw",
            f"/mounts/{BULK_DELETE_MOUNT_NAME}/clip_2.braw",
        ]

    @pytest.mark.asyncio
    async def test_raises_value_error_and_deletes_nothing_when_an_id_is_missing(self):
        client = _bulk_delete_client(1)  # only clip_unique_id=1 exists
        session = make_session(make_profile(), client=client)

        with pytest.raises(ValueError, match=r"\[2\]"):
            await session.delete_clips([1, 2], confirm=True)

        assert client.delete_calls == []

    @pytest.mark.asyncio
    async def test_full_flow_success_all_deleted(self):
        client = _bulk_delete_client(1, 2, 3)
        session = make_session(make_profile(), client=client)

        result = await session.delete_clips([1, 2, 3], confirm=True)

        assert isinstance(result, BulkDeleteResult)
        assert [c.clip_unique_id for c in result.deleted] == [1, 2, 3]
        assert result.failed == ()
        assert len(client.delete_calls) == 3

    @pytest.mark.asyncio
    async def test_partial_failure_continues_and_is_reported(self):
        client = _bulk_delete_client(1, 2, 3, exists_before={2: False})
        session = make_session(make_profile(), client=client)

        result = await session.delete_clips([1, 2, 3], confirm=True)

        assert [c.clip_unique_id for c in result.deleted] == [1, 3]
        assert len(result.failed) == 1
        failed_id, failed_exc = result.failed[0]
        assert failed_id == 2
        assert isinstance(failed_exc, BMDVerificationError)
        # clip 3 must still be attempted even though clip 2 failed first.
        assert client.delete_calls == [
            f"/mounts/{BULK_DELETE_MOUNT_NAME}/clip_1.braw",
            f"/mounts/{BULK_DELETE_MOUNT_NAME}/clip_3.braw",
        ]

    @pytest.mark.asyncio
    async def test_logs_error_for_each_failed_clip(self, caplog):
        client = _bulk_delete_client(1, 2, exists_before={2: False})
        session = make_session(make_profile(), client=client)

        with caplog.at_level(logging.ERROR):
            await session.delete_clips([1, 2], confirm=True)

        messages = [record.message for record in caplog.records]
        assert any("clip_unique_id=2" in m and "failed" in m for m in messages)
