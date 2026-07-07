"""Unit tests for :mod:`bmd_ble.session`.

Mocks BMDCameraController and NotificationRouter — no real BLE.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bmd_ble.exceptions import BMDVerificationError
from bmd_ble.protocol.types import DataType
from bmd_ble.session import CameraSession, require_recording_fields

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v7.9"


def make_profile(**overrides) -> SimpleNamespace:
    defaults = dict(
        model_key=MODEL_KEY,
        firmware=FIRMWARE,
        recording_category=0x0A,
        recording_parameter=0x01,
        recording_data_type=DataType.BOOL,
        recording_reserved=0x01,
        recording_start_value=2,
        recording_stop_value=0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_session(profile: SimpleNamespace) -> CameraSession:
    """Build a CameraSession with a fake profile and mocked collaborators,
    bypassing __init__'s real CameraProfile.for_model lookup."""
    session = CameraSession.__new__(CameraSession)
    session.profile = profile
    session.echo_timeout_s = 2.0
    session._router = MagicMock()
    session._router.wait_for = AsyncMock()
    session._controller = AsyncMock()
    return session


class TestRequireRecordingFields:
    def test_passes_when_all_fields_present(self):
        require_recording_fields(make_profile())

    def test_raises_naming_missing_fields(self):
        profile = make_profile(recording_category=None, recording_start_value=None)
        with pytest.raises(ValueError, match="recording_category, recording_start_value"):
            require_recording_fields(profile)


class TestSetRecordingState:
    @pytest.mark.asyncio
    async def test_record_start_succeeds_on_matching_echo(self):
        session = make_session(make_profile())
        session._router.wait_for.return_value = (
            SimpleNamespace(category=0x0A, parameter=0x01),
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
            SimpleNamespace(category=0x0A, parameter=0x01),
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
            SimpleNamespace(category=0x0A, parameter=0x01),
            bytes([0x00]),
        )

        with pytest.raises(BMDVerificationError, match="expected True"):
            await session.record_start()

    @pytest.mark.asyncio
    async def test_raises_before_writing_when_recording_fields_missing(self):
        session = make_session(make_profile(recording_category=None))

        with pytest.raises(ValueError, match="recording_category"):
            await session.record_start()

        session._controller.write_outgoing_control.assert_not_awaited()
