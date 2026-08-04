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
    RECORD_PROPERTY,
    Clip,
    Format,
    RestCameraSession,
    StorageState,
    SupportedFormat,
)

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
    ):
        self.responses = responses
        self.errors = errors or {}
        self.put_responses = put_responses or {}
        self.exists_responses = exists_responses or {}
        self.calls: list[str] = []
        self.put_calls: list[tuple[str, object]] = []
        self.exists_calls: list[str] = []
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

    async def exists(self, path: str, *, api_prefixed: bool = True) -> bool:
        self.exists_calls.append(path)
        self.api_prefixed_calls[path] = api_prefixed
        if path in self.errors:
            raise self.errors[path]
        return self.exists_responses.get(path, False)


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
    session._log = logging.getLogger("test.rest_session")
    session._session = None
    session._owns_session = True
    session._client = client
    session._router = RestEventRouter(on_event=session._on_event)
    session.is_recording = None
    session._recording_stopped = asyncio.Event()
    return session


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


def _storage_client(
    *, active: bool, remaining_record_time: int, extra_responses: dict[str, object] | None = None
) -> FakeRestClient:
    device = {
        "activeDisk": active,
        "clipCount": 0,
        "deviceName": "sd0" if active else "",
        "index": 0,
        "remainingRecordTime": remaining_record_time,
        "remainingSpace": 123,
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
    async def test_raises_bmd_storage_error_when_no_remaining_record_time(self):
        client = _storage_client(active=True, remaining_record_time=0)
        session = make_session(make_profile_with_record_confirmed(), client=client)

        with pytest.raises(BMDStorageError, match="no remaining record time"):
            await session.record_start()

        assert client.put_calls == []

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
        readback. __aenter__ must subscribe to both properties it later
        arms, not just RECORD_PROPERTY."""
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
