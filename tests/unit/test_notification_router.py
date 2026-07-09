"""Unit tests for :mod:`bmd_ble.notification_router`."""

import asyncio
from types import SimpleNamespace

import pytest

from bmd_ble.notification_router import NotificationRouter
from bmd_ble.protocol.codec import CommandHeader, Operation, decode_packet, encode_packet
from bmd_ble.protocol.types import DataType

CATEGORY = 0x0A
PARAMETER = 0x01


def _packet(value: int) -> bytearray:
    header = CommandHeader(
        destination=0xFF,
        command_id=0x00,
        category=CATEGORY,
        parameter=PARAMETER,
        data_type=DataType.INT8,
        operation=Operation.CAMERA_REPORT,
        reserved=0x01,
    )
    return bytearray(encode_packet(header, payload=bytes([value])))


class TestHandleIncoming:
    def test_ignores_undecodable_notifications(self):
        router = NotificationRouter()
        router.handle_incoming(SimpleNamespace(), bytearray([0x03]))

        assert router._latest == {}

    def test_stores_latest_decoded_packet_by_category_parameter(self):
        router = NotificationRouter()
        router.handle_incoming(SimpleNamespace(), _packet(0x02))

        header, payload = router._latest[(CATEGORY, PARAMETER)]
        assert header.category == CATEGORY
        assert header.parameter == PARAMETER
        assert payload == bytes([0x02])


class TestWaitFor:
    @pytest.mark.asyncio
    async def test_returns_match_that_arrives_after_wait_for_is_called(self):
        router = NotificationRouter()
        router.arm(CATEGORY, PARAMETER)

        async def deliver_later():
            await asyncio.sleep(0.01)
            router.handle_incoming(SimpleNamespace(), _packet(0x02))

        asyncio.create_task(deliver_later())
        result = await router.wait_for(CATEGORY, PARAMETER, timeout=1.0)

        assert result is not None
        _header, payload = result
        assert payload == bytes([0x02])

    @pytest.mark.asyncio
    async def test_returns_match_buffered_before_wait_for_is_called(self):
        """Buffering starts at subscribe time, not at wait_for time."""
        router = NotificationRouter()
        router.arm(CATEGORY, PARAMETER)
        router.handle_incoming(SimpleNamespace(), _packet(0x00))

        result = await router.wait_for(CATEGORY, PARAMETER, timeout=1.0)

        assert result is not None
        _header, payload = result
        assert payload == bytes([0x00])

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self):
        router = NotificationRouter()
        router.arm(CATEGORY, PARAMETER)

        result = await router.wait_for(CATEGORY, PARAMETER, timeout=0.05)

        assert result is None

    @pytest.mark.asyncio
    async def test_arm_clears_a_stale_match_from_before_it_was_called(self):
        router = NotificationRouter()
        router.handle_incoming(SimpleNamespace(), _packet(0x02))  # stale, unrelated match

        router.arm(CATEGORY, PARAMETER)
        result = await router.wait_for(CATEGORY, PARAMETER, timeout=0.05)

        assert result is None


def test_decode_packet_still_works_directly():
    """Sanity check the shared helper used to build test fixtures above."""
    header, payload = decode_packet(bytes(_packet(0x02)))
    assert header.category == CATEGORY
    assert payload == bytes([0x02])
