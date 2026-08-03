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
from bmd_camera.exceptions import BMDConnectionError, BMDStorageError, BMDUnsupportedError
from bmd_camera.rest.events import RestEventRouter
from bmd_camera.rest.exceptions import BMDRestError
from bmd_camera.rest.session import (
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


class FakeRestClient:
    """Minimal stand-in for RestClient: `.get(path)` returns a canned body,
    or raises a canned exception (for `errors`) — e.g. simulating a real
    404 without a real network."""

    def __init__(self, responses: dict[str, object], *, errors: dict[str, Exception] | None = None):
        self.responses = responses
        self.errors = errors or {}
        self.calls: list[str] = []

    async def get(self, path: str):
        self.calls.append(path)
        if path in self.errors:
            raise self.errors[path]
        return self.responses[path]


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


class TestWaitWhileRecording:
    @pytest.mark.asyncio
    async def test_returns_true_immediately_when_not_recording(self):
        session = make_session(make_profile())
        session.is_recording = False

        assert await session.wait_while_recording(timeout=0.05) is True

    @pytest.mark.asyncio
    async def test_returns_true_immediately_when_unknown(self):
        session = make_session(make_profile())
        assert session.is_recording is None

        assert await session.wait_while_recording(timeout=0.05) is True

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout_while_still_recording(self):
        session = make_session(make_profile())
        session.is_recording = True

        assert await session.wait_while_recording(timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_returns_true_when_stop_event_arrives_before_timeout(self):
        session = make_session(make_profile())
        session.is_recording = True

        async def stop_later():
            await asyncio.sleep(0.01)
            session._on_event("/transports/0/record", {"recording": False})

        asyncio.create_task(stop_later())

        assert await session.wait_while_recording(timeout=1.0) is True
        assert session.is_recording is False


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

    def __init__(self):
        self.responses: dict[str, FakeResponse] = {}
        self.ws = FakeWebSocket()
        self.requests: list[tuple[str, str]] = []
        self.closed = False

    def request(self, method: str, url: str, *, json=None, timeout=None):
        self.requests.append((method, url))
        for path, response in self.responses.items():
            if url.endswith(path):
                return response
        raise AssertionError(f"No fake response configured for {url}")

    async def ws_connect(self, url: str, timeout=None):
        return self.ws

    async def close(self) -> None:
        self.closed = True


class TestConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_aenter_subscribes_to_record_property(self):
        fake_session = FakeAiohttpSession()
        session = RestCameraSession("cam.local", MODEL_KEY, FIRMWARE, session=fake_session)

        async with session:
            pass

        assert fake_session.ws.sent == [
            {
                "type": "request",
                "id": 0,
                "data": {"action": "subscribe", "properties": ["/transports/0/record"]},
            }
        ]

    @pytest.mark.asyncio
    async def test_injected_session_not_closed_on_exit(self):
        fake_session = FakeAiohttpSession()
        session = RestCameraSession("cam.local", MODEL_KEY, FIRMWARE, session=fake_session)

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
