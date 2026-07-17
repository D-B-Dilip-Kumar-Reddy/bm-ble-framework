"""Unit tests for :mod:`bmd_ble.session`.

Mocks BMDCameraController and NotificationRouter — no real BLE. The profile
is a real CameraProfile built via the lenient `_from_raw` (see
test_camera_profile.py) so `require_command` behaves exactly as in
production.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bmd_ble.session as session_module
from bmd_ble.camera_profile import CameraProfile
from bmd_ble.exceptions import BMDVerificationError
from bmd_ble.protocol.codec import CommandHeader, Operation, encode_packet
from bmd_ble.protocol.types import DataType
from bmd_ble.session import CameraSession
from bmd_ble.timecode import TIMECODE_CATEGORY, TIMECODE_PARAMETER, Timecode

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v7.9"


def _bcd(value: int) -> int:
    tens, ones = divmod(value, 10)
    return (tens << 4) | ones


def _timecode_packet(*, frames: int, seconds: int, minutes: int, hours: int) -> bytearray:
    header = CommandHeader(
        destination=0xFF,
        command_id=0x00,
        reserved=0xFF,
        category=TIMECODE_CATEGORY,
        parameter=TIMECODE_PARAMETER,
        data_type=DataType.INT32,
        operation=Operation.ASSIGN,
    )
    payload = bytes(_bcd(v) for v in (frames, seconds, minutes, hours))
    return bytearray(encode_packet(header, payload))


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
    session._latest_timecode = None
    session.last_start_timecode = None
    session.last_stop_timecode = None
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


class TestTimecodeCapture:
    def _confirmed_echo(self, *, value: int):
        return (MagicMock(category=0x0A, parameter=0x01), bytes([value]))

    @pytest.mark.asyncio
    async def test_record_start_sets_canonical_zero_timecode(self):
        """TIMECODE resets to 00:00:00:00 on real hardware when recording
        starts (confirmed on POCKET_6K_G2 v7.9 and POCKET_6K_PRO v8.6) — a
        confirmed record_start must set this canonical zero regardless of
        whatever `_latest_timecode` currently holds, since that could be a
        stale leftover reading from the *previous* clip's end."""
        session = make_session(make_profile())
        session._router.wait_for.return_value = self._confirmed_echo(value=2)
        session._latest_timecode = Timecode(hours=1, minutes=0, seconds=0, frames=0)

        await session.record_start()

        assert session.last_start_timecode == Timecode(hours=0, minutes=0, seconds=0, frames=0)
        assert session.last_stop_timecode is None

    @pytest.mark.asyncio
    async def test_record_start_sets_canonical_zero_even_with_no_timecode_seen_yet(self):
        session = make_session(make_profile())
        session._router.wait_for.return_value = self._confirmed_echo(value=2)
        # session._latest_timecode already None from make_session

        await session.record_start()

        assert session.last_start_timecode == Timecode(hours=0, minutes=0, seconds=0, frames=0)

    @pytest.mark.asyncio
    async def test_record_stop_snapshots_latest_timecode(self):
        session = make_session(make_profile())
        session._router.wait_for.return_value = self._confirmed_echo(value=0)
        tc = Timecode(hours=1, minutes=0, seconds=10, frames=0)
        session._latest_timecode = tc

        await session.record_stop()

        assert session.last_stop_timecode == tc

    @pytest.mark.asyncio
    async def test_record_stop_snapshots_none_when_no_timecode_seen_yet(self):
        session = make_session(make_profile())
        session._router.wait_for.return_value = self._confirmed_echo(value=0)
        # session._latest_timecode already None from make_session

        await session.record_stop()

        assert session.last_stop_timecode is None

    @pytest.mark.asyncio
    async def test_failed_verification_does_not_snapshot_timecode(self):
        session = make_session(make_profile())
        session._router.wait_for.return_value = None  # no echo -> raises
        session._latest_timecode = Timecode(hours=0, minutes=0, seconds=5, frames=0)

        with pytest.raises(BMDVerificationError):
            await session.record_start()

        assert session.last_start_timecode is None

    def test_handle_timecode_decodes_and_stores(self):
        session = make_session(make_profile())
        packet = _timecode_packet(frames=10, seconds=53, minutes=12, hours=9)

        session._handle_timecode(MagicMock(), packet)

        assert session._latest_timecode == Timecode(hours=9, minutes=12, seconds=53, frames=10)

    def test_handle_timecode_ignores_malformed_data(self):
        session = make_session(make_profile())

        session._handle_timecode(MagicMock(), bytearray([0x01, 0x02]))

        assert session._latest_timecode is None


class TestLastClipDurationSeconds:
    def test_returns_none_when_start_missing(self):
        session = make_session(make_profile())
        session.last_stop_timecode = Timecode(hours=0, minutes=0, seconds=5, frames=0)

        assert session.last_clip_duration_seconds() is None

    def test_returns_none_when_stop_missing(self):
        session = make_session(make_profile())
        session.last_start_timecode = Timecode(hours=0, minutes=0, seconds=0, frames=0)

        assert session.last_clip_duration_seconds() is None

    def test_computes_duration_when_both_present(self):
        session = make_session(make_profile())
        session.last_start_timecode = Timecode(hours=0, minutes=0, seconds=0, frames=0)
        session.last_stop_timecode = Timecode(hours=0, minutes=0, seconds=7, frames=0)

        assert session.last_clip_duration_seconds() == 7.0

    def test_returns_none_when_stop_not_after_start(self):
        session = make_session(make_profile())
        session.last_start_timecode = Timecode(hours=0, minutes=0, seconds=10, frames=0)
        session.last_stop_timecode = Timecode(hours=0, minutes=0, seconds=5, frames=0)

        assert session.last_clip_duration_seconds() is None


class TestAenter:
    @pytest.mark.asyncio
    async def test_settles_after_subscribing_before_returning(self):
        """A just-connected camera floods the link with an initial info dump;
        __aenter__ must wait connect_settle_s (after subscribing, before
        returning) so the first command isn't sent into that backlog — see
        real-hardware evidence in docs/session_and_verification.md."""
        fake_controller = AsyncMock()
        with (
            patch.object(
                session_module, "scan_for_camera", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(session_module, "BMDCameraController", return_value=fake_controller),
            patch.object(session_module.asyncio, "sleep", new=AsyncMock()) as mock_sleep,
        ):
            session = CameraSession(MODEL_KEY, FIRMWARE, connect_settle_s=1.5)
            result = await session.__aenter__()

        assert result is session
        fake_controller.connect.assert_awaited_once()
        fake_controller.subscribe_incoming.assert_awaited_once()
        fake_controller.subscribe_timecode.assert_awaited_once()
        mock_sleep.assert_awaited_once_with(1.5)
