"""Unit tests for :mod:`bmd_camera.rest.events`.

Mirrors tests/unit/test_notification_router.py's coverage shape (same
freshness/staleness contract, see RestEventRouter's docstring), keyed by
property string instead of (category, parameter), plus connection-lifecycle
tests (connect/disconnect/subscribe/resubscribe/generation-guard) using a
fake WebSocket — no real network.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from bmd_camera.rest.events import RestEventRouter, is_property_event
from bmd_camera.rest.exceptions import BMDConnectionError

PROP = "/transports/0/record"


def _event(prop: str, value: object) -> dict:
    return {
        "type": "event",
        "data": {"action": "propertyValueChanged", "property": prop, "value": value},
    }


class FakeWSMessage:
    def __init__(self, type_: aiohttp.WSMsgType, data: object):
        self.type = type_
        self._data = data

    def json(self):
        return self._data


class FakeWebSocket:
    """Async-iterable fake for `aiohttp.ClientWebSocketResponse`. Messages
    are pushed onto an internal queue and consumed by the router's
    background reader exactly like a real socket's incoming frames."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    def push(self, message: dict) -> None:
        self._queue.put_nowait(FakeWSMessage(aiohttp.WSMsgType.TEXT, message))

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


class FakeSession:
    def __init__(self, ws: FakeWebSocket | None = None, *, raises: Exception | None = None):
        self.ws = ws
        self.raises = raises
        self.connect_urls: list[str] = []

    async def ws_connect(self, url: str, timeout=None):
        self.connect_urls.append(url)
        if self.raises is not None:
            raise self.raises
        return self.ws


class TestIsPropertyEvent:
    def test_accepts_well_formed_property_value_changed_event(self):
        assert is_property_event(_event(PROP, {"recording": True})) is True

    def test_rejects_response_message(self):
        message = {"type": "response", "id": 0, "data": {"success": True}}
        assert is_property_event(message) is False

    def test_rejects_unrecognised_event_action(self):
        message = {"type": "event", "data": {"action": "websocketOpened"}}
        assert is_property_event(message) is False

    def test_rejects_non_dict(self):
        assert is_property_event("not a dict") is False
        assert is_property_event(None) is False

    def test_rejects_event_missing_property(self):
        message = {"type": "event", "data": {"action": "propertyValueChanged", "value": {}}}
        assert is_property_event(message) is False


class TestBuffering:
    def test_ignores_unrecognised_messages(self):
        router = RestEventRouter()
        router.handle_event({"type": "response", "id": 0, "data": {"success": True}})

        assert router._latest == {}

    def test_stores_latest_value_by_property(self):
        router = RestEventRouter()
        router.handle_event(_event(PROP, {"recording": True}))

        seq, value = router._latest[PROP]
        assert seq == 1
        assert value == {"recording": True}

    def test_on_event_callback_invoked_for_every_routed_event(self):
        seen = []
        router = RestEventRouter(on_event=lambda prop, value: seen.append((prop, value)))

        router.handle_event(_event(PROP, {"recording": True}))
        router.handle_event({"type": "response", "id": 0, "data": {"success": True}})

        assert seen == [(PROP, {"recording": True})]


class TestArmAndWaitFor:
    @pytest.mark.asyncio
    async def test_returns_value_that_arrives_after_wait_for_is_called(self):
        router = RestEventRouter()
        router.arm(PROP)

        async def deliver_later():
            await asyncio.sleep(0.01)
            router.handle_event(_event(PROP, {"recording": True}))

        asyncio.create_task(deliver_later())
        result = await router.wait_for(PROP, timeout=1.0)

        assert result == {"recording": True}

    @pytest.mark.asyncio
    async def test_returns_value_buffered_before_wait_for_is_called(self):
        router = RestEventRouter()
        router.arm(PROP)
        router.handle_event(_event(PROP, {"recording": True}))

        result = await router.wait_for(PROP, timeout=1.0)

        assert result == {"recording": True}

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self):
        router = RestEventRouter()
        router.arm(PROP)

        result = await router.wait_for(PROP, timeout=0.05)

        assert result is None

    @pytest.mark.asyncio
    async def test_arm_clears_a_stale_match_from_before_it_was_called(self):
        router = RestEventRouter()
        router.handle_event(_event(PROP, {"recording": True}))  # stale, pre-arm

        router.arm(PROP)
        result = await router.wait_for(PROP, timeout=0.05)

        assert result is None

    @pytest.mark.asyncio
    async def test_ignores_duplicate_retransmit_of_previously_consumed_value(self):
        """Same regression NotificationRouter guards against: a duplicate
        delivery of the value already visible at arm() time must not
        satisfy wait_for, even though it arrives after arm()."""
        router = RestEventRouter()

        router.arm(PROP)
        router.handle_event(_event(PROP, {"recording": True}))
        first = await router.wait_for(PROP, timeout=1.0)
        assert first == {"recording": True}

        router.arm(PROP)
        router.handle_event(_event(PROP, {"recording": True}))  # stale duplicate
        router.handle_event(_event(PROP, {"recording": False}))  # genuine new value
        second = await router.wait_for(PROP, timeout=1.0)

        assert second == {"recording": False}

    @pytest.mark.asyncio
    async def test_fresh_value_accepted_even_if_it_matches_an_older_unconsumed_value(self):
        router = RestEventRouter()

        router.arm(PROP)
        router.handle_event(_event(PROP, {"recording": True}))
        first = await router.wait_for(PROP, timeout=1.0)
        assert first == {"recording": True}

        router.handle_event(_event(PROP, {"recording": False}))  # never consumed

        router.arm(PROP)  # no fresh delivery arrives for this arm
        timed_out = await router.wait_for(PROP, timeout=0.05)
        assert timed_out is None

        router.arm(PROP)
        router.handle_event(_event(PROP, {"recording": True}))  # genuinely fresh
        second = await router.wait_for(PROP, timeout=1.0)

        assert second == {"recording": True}


class TestConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_connect_failure_raises_bmd_connection_error(self):
        session = FakeSession(raises=aiohttp.ClientConnectionError("refused"))
        router = RestEventRouter()

        with pytest.raises(BMDConnectionError, match="ws://cam.local/ws"):
            await router.connect(session, "ws://cam.local/ws")

    @pytest.mark.asyncio
    async def test_subscribe_without_connect_raises(self):
        router = RestEventRouter()

        with pytest.raises(BMDConnectionError, match="not connected"):
            await router.subscribe(PROP)

    @pytest.mark.asyncio
    async def test_subscribe_sends_the_documented_request_shape(self):
        ws = FakeWebSocket()
        session = FakeSession(ws)
        router = RestEventRouter()
        await router.connect(session, "ws://cam.local/ws")

        await router.subscribe(PROP, request_id=7)

        assert ws.sent == [
            {"type": "request", "id": 7, "data": {"action": "subscribe", "properties": [PROP]}}
        ]
        await router.disconnect()

    @pytest.mark.asyncio
    async def test_events_delivered_after_connect_reach_wait_for(self):
        ws = FakeWebSocket()
        session = FakeSession(ws)
        router = RestEventRouter()
        await router.connect(session, "ws://cam.local/ws")
        await router.subscribe(PROP)

        router.arm(PROP)
        ws.push(_event(PROP, {"recording": True}))

        result = await router.wait_for(PROP, timeout=1.0)

        assert result == {"recording": True}
        await router.disconnect()

    @pytest.mark.asyncio
    async def test_reconnect_resubscribes_every_prior_property(self):
        """Subscriptions are per-connection — a reconnect must resubscribe,
        the same lesson docs/ble/winrt_ble_connection_hardening.md records
        for BLE CCCD subscriptions surviving an OS-level reconnect."""
        first_ws = FakeWebSocket()
        second_ws = FakeWebSocket()
        session = FakeSession(first_ws)
        router = RestEventRouter()

        await router.connect(session, "ws://cam.local/ws")
        await router.subscribe(PROP)

        session.ws = second_ws
        await router.connect(session, "ws://cam.local/ws")

        assert second_ws.sent == [
            {"type": "request", "id": 0, "data": {"action": "subscribe", "properties": [PROP]}}
        ]
        await router.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_is_safe_when_never_connected(self):
        router = RestEventRouter()
        await router.disconnect()  # must not raise

    @pytest.mark.asyncio
    async def test_stale_generation_reader_does_not_route_messages(self):
        """A reader task from a superseded connection must never deliver
        into the router after a newer connect() — the connection-generation
        guard docs/ble/winrt_ble_connection_hardening.md documents for BLE."""
        router = RestEventRouter()
        ws = FakeWebSocket()
        router._ws = ws
        stale_generation = router._generation  # 0

        router._generation += 1  # simulate a newer connect() having happened

        ws.push(_event(PROP, {"recording": True}))
        await ws.close()  # ends the async-for loop after the one message
        await router._read_loop(stale_generation)

        assert router._latest == {}


class TestRealServerRegressions:
    """Fixtures taken verbatim from a real `tools/rest/watch_events.py` run
    against `POCKET_6K_PRO v8.6` over USB (2026-08-04), subscribed to all 48
    `websocket_properties` from its profile — the first time this repo
    captured full `propertyValueChanged` event bodies rather than just
    subscription success/failure. Confirms the shape documented in
    docs/rest/transport.md's "Library surface (Phase 2)" section is real,
    not just spec-derived. Pinned here so a future refactor can't silently
    break parsing for any of these three shapes."""

    def test_handles_real_system_format_event(self):
        """/system/format's value matches the Notification.yaml Format
        schema exactly, including nested recordResolution/sensorResolution."""
        router = RestEventRouter()
        message = {
            "type": "event",
            "data": {
                "action": "propertyValueChanged",
                "property": "/system/format",
                "value": {
                    "codec": "ProRes:Proxy",
                    "frameRate": "23.98",
                    "maxOffSpeedFrameRate": 60,
                    "minOffSpeedFrameRate": 5,
                    "offSpeedEnabled": False,
                    "offSpeedFrameRate": 24,
                    "recordResolution": {"height": 2160, "width": 4096},
                    "sensorResolution": {"height": 3024, "width": 5744},
                },
            },
        }

        router.handle_event(message)

        _seq, value = router._latest["/system/format"]
        assert value["codec"] == "ProRes:Proxy"
        assert value["recordResolution"] == {"height": 2160, "width": 4096}

    def test_handles_real_system_event_with_none_value(self):
        """/system's event value is None on real hardware, not a dict —
        consistent with GET /system returning 204/empty. A caller cannot
        assume every event's value is a mapping."""
        router = RestEventRouter()
        message = {
            "type": "event",
            "data": {"action": "propertyValueChanged", "property": "/system", "value": None},
        }

        router.handle_event(message)

        _seq, value = router._latest["/system"]
        assert value is None

    def test_handles_real_media_workingset_event(self):
        """/media/workingset is not in Notification.yaml's documented
        deviceProperty enum but subscribed and emitted real content on real
        hardware anyway — the same "undocumented but real" pattern already
        established for /camera/id, /presets, /presets/active."""
        router = RestEventRouter()
        message = {
            "type": "event",
            "data": {
                "action": "propertyValueChanged",
                "property": "/media/workingset",
                "value": {
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
                },
            },
        }

        router.handle_event(message)

        _seq, value = router._latest["/media/workingset"]
        assert value["size"] == 3
        assert value["workingset"][1]["deviceName"] == "sd0"
