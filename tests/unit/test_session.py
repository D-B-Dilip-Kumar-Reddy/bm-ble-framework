"""Unit tests for :mod:`bmd_ble.session`.

Mocks BMDCameraController and NotificationRouter — no real BLE. The profile
is a real CameraProfile built via the lenient `_from_raw` (see
test_camera_profile.py) so `require_command` behaves exactly as in
production.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from bmd_ble.camera_profile import CameraProfile
from bmd_ble.exceptions import BMDVerificationError
from bmd_ble.session import CameraSession

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v7.9"


def make_profile(recording_block: dict | str = "default") -> CameraProfile:
    """A real CameraProfile with a controllable `commands.recording` block.

    Pass ``recording_block=None`` for a profile without the block, or a dict
    to override the default sniffer-verified G2 values.
    """
    if recording_block == "default":
        recording_block = {
            "category": 0x0A,
            "parameter": 0x01,
            "data_type": "INT8",
            "reserved": 0x01,
            "values": {"start": 2, "stop": 0},
        }
    raw = {"_meta": {"model": "Pocket 6K G2", "ble_name": "A:TEST"}}
    if recording_block is not None:
        raw["commands"] = {"recording": recording_block}
    return CameraProfile._from_raw(MODEL_KEY, FIRMWARE, raw)


def make_session(profile: CameraProfile) -> CameraSession:
    """Build a CameraSession with a fake profile and mocked collaborators,
    bypassing __init__'s real CameraProfile.for_model lookup."""
    session = CameraSession.__new__(CameraSession)
    session.profile = profile
    session.echo_timeout_s = 2.0
    session._router = MagicMock()
    session._router.wait_for = AsyncMock()
    session._controller = AsyncMock()
    return session


class TestSetRecordingState:
    @pytest.mark.asyncio
    async def test_record_start_succeeds_on_matching_echo(self):
        session = make_session(make_profile())
        session._router.wait_for.return_value = (
            MagicMock(category=0x0A, parameter=0x01),
            bytes([0x02]),
        )

        await session.record_start()

        session._router.arm.assert_called_once_with(0x0A, 0x01)
        session._controller.write_outgoing_control.assert_awaited_once()
        session._router.wait_for.assert_awaited_once_with(0x0A, 0x01, timeout=2.0)

    @pytest.mark.asyncio
    async def test_record_stop_succeeds_on_matching_echo(self):
        session = make_session(make_profile())
        session._router.wait_for.return_value = (
            MagicMock(category=0x0A, parameter=0x01),
            bytes([0x00]),
        )

        await session.record_stop()

    @pytest.mark.asyncio
    async def test_record_start_raises_when_no_echo_arrives(self):
        session = make_session(make_profile())
        session._router.wait_for.return_value = None

        with pytest.raises(BMDVerificationError, match="no echo received"):
            await session.record_start()

    @pytest.mark.asyncio
    async def test_record_start_raises_when_echo_confirms_wrong_state(self):
        session = make_session(make_profile())
        # Echo says still stopped (leading byte 0) even though we asked to start.
        session._router.wait_for.return_value = (
            MagicMock(category=0x0A, parameter=0x01),
            bytes([0x00]),
        )

        with pytest.raises(BMDVerificationError, match="expected True"):
            await session.record_start()

    @pytest.mark.asyncio
    async def test_raises_before_writing_when_recording_block_missing(self):
        session = make_session(make_profile(recording_block=None))

        with pytest.raises(ValueError, match="no 'recording' command block"):
            await session.record_start()

        session._controller.write_outgoing_control.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_before_writing_when_value_missing(self):
        block = {
            "category": 0x0A,
            "parameter": 0x01,
            "data_type": "INT8",
            "values": {"start": 2},
        }
        session = make_session(make_profile(recording_block=block))

        with pytest.raises(ValueError, match="missing.*values: stop"):
            await session.record_start()

        session._controller.write_outgoing_control.assert_not_awaited()
