"""Unit tests for :mod:`bmd_ble.session`.

Mocks BMDCameraController and NotificationRouter — no real BLE. The profile
is a real CameraProfile built via the lenient `_from_raw` (see
test_camera_profile.py) so `require_command` behaves exactly as in
production.
"""

import asyncio
import time
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


def _recording_packet(value: int) -> bytearray:
    """A recording-category CAMERA_REPORT, matching the default profile's
    category=0x0A/parameter=0x01/INT8 (see `make_profile`'s default block)."""
    header = CommandHeader(
        destination=0xFF,
        command_id=0x00,
        reserved=0x01,
        category=0x0A,
        parameter=0x01,
        data_type=DataType.INT8,
        operation=Operation.CAMERA_REPORT,
    )
    return bytearray(encode_packet(header, payload=bytes([value])))


def _storage_packet(value: int, *, byte_offset: int = 1) -> bytearray:
    """A storage-category CAMERA_REPORT matching the default profile's
    write_margin_warning block (category=0x09/parameter=0x01/INT8,
    meaningful byte at offset 1) — mirrors the real 3-byte capture
    (`00 01 00` / `00 FE 00`), with `value` placed at `byte_offset` and the
    other two bytes zero."""
    header = CommandHeader(
        destination=0xFF,
        command_id=0x00,
        reserved=0x00,
        category=0x09,
        parameter=0x01,
        data_type=DataType.INT8,
        operation=Operation.CAMERA_REPORT,
    )
    payload = bytearray(3)
    payload[byte_offset] = value & 0xFF
    return bytearray(encode_packet(header, payload=bytes(payload)))


def _codec_quality_packet(codec_id: int, variant_id: int) -> bytearray:
    """A codec_quality-category CAMERA_REPORT, matching
    `make_settings_profile`'s default block (category=0x0A/parameter=0x00/
    INT8) — mirrors the real capture (e.g. `03 03` for BRAW 5:1)."""
    header = CommandHeader(
        destination=0xFF,
        command_id=0x00,
        reserved=0x00,
        category=0x0A,
        parameter=0x00,
        data_type=DataType.INT8,
        operation=Operation.CAMERA_REPORT,
    )
    return bytearray(encode_packet(header, payload=bytes([codec_id, variant_id])))


def make_profile(
    recording_block: dict | str = "default", storage_block: dict | str = "default"
) -> CameraProfile:
    """A real CameraProfile with controllable `commands.recording` and
    `storage.write_margin_warning` blocks.

    Pass ``None`` for either to build a profile lacking that block, or a
    dict to override the defaults.
    """
    if recording_block == "default":
        recording_block = {
            "category": 0x0A,
            "parameter": 0x01,
            "data_type": "INT8",
            "reserved": 0x01,
            "values": {"start": 2, "stop": 0},
        }
    if storage_block == "default":
        storage_block = {
            "category": 0x09,
            "parameter": 0x01,
            "data_type": "INT8",
            "byte_offset": 1,
            "values": {"nominal": 1, "low_margin": -2},
        }
    raw = {"_meta": {"model": "Pocket 6K G2", "ble_name": "A:TEST"}}
    if recording_block is not None:
        raw["commands"] = {"recording": recording_block}
    if storage_block is not None:
        raw["storage"] = {"write_margin_warning": storage_block}
    return CameraProfile._from_raw(MODEL_KEY, FIRMWARE, raw)


def make_session(profile: CameraProfile) -> CameraSession:
    """Build a CameraSession with a fake profile and mocked collaborators,
    bypassing __init__'s real CameraProfile.for_model lookup."""
    session = CameraSession.__new__(CameraSession)
    session.profile = profile
    session.echo_timeout_s = 2.0
    session.write_margin_window_s = 2.0
    session._router = MagicMock()
    session._router.wait_for = AsyncMock()
    session._controller = AsyncMock()
    session._latest_timecode = None
    session.last_start_timecode = None
    session.last_stop_timecode = None
    session.is_recording = None
    session.last_stop_reason = None
    session.last_stop_signal = None
    session._last_low_margin_at = None
    session._unexpected_stop_event = asyncio.Event()
    session._pending_command = False
    session.last_known_codec_variant = None
    session.last_known_recording_format = None
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


class TestObserveRecordingState:
    """Real-hardware evidence (slow SD card write speed): the camera can
    autonomously stop recording without us ever sending a stop command.
    `_observe_recording_state` is what lets CameraSession notice this from
    the notification stream instead of only finding out the next time
    record_stop() is called. See docs/recording.md."""

    def test_updates_is_recording_from_any_recording_notification(self):
        session = make_session(make_profile())

        session._observe_recording_state(_recording_packet(2))
        assert session.is_recording is True

        session._observe_recording_state(_recording_packet(0))
        assert session.is_recording is False

    def test_ignores_non_recording_notifications(self):
        session = make_session(make_profile())

        session._observe_recording_state(_timecode_packet(frames=0, seconds=0, minutes=0, hours=0))

        assert session.is_recording is None

    def test_ignores_malformed_data(self):
        session = make_session(make_profile())

        session._observe_recording_state(bytearray([0x01, 0x02]))

        assert session.is_recording is None

    def test_unsolicited_stop_flags_unexpected_and_sets_event(self):
        session = make_session(make_profile())
        session.is_recording = True  # already recording, no command in flight
        tc = Timecode(hours=0, minutes=0, seconds=3, frames=0)
        session._latest_timecode = tc

        session._observe_recording_state(_recording_packet(0))

        assert session.is_recording is False
        assert session.last_stop_reason == "unexpected"
        assert session.last_stop_timecode == tc
        assert session._unexpected_stop_event.is_set()

    def test_stop_while_pending_command_is_not_flagged_unexpected(self):
        """The echo for our *own* record_stop() must not be mistaken for an
        unsolicited stop — see _pending_command in _set_recording_state."""
        session = make_session(make_profile())
        session.is_recording = True
        session._pending_command = True

        session._observe_recording_state(_recording_packet(0))

        assert session.is_recording is False
        assert session.last_stop_reason is None
        assert not session._unexpected_stop_event.is_set()

    def test_start_transition_never_flags_unexpected(self):
        session = make_session(make_profile())
        session.is_recording = False

        session._observe_recording_state(_recording_packet(2))

        assert session.is_recording is True
        assert session.last_stop_reason is None
        assert not session._unexpected_stop_event.is_set()

    def test_no_op_without_a_recording_command_block(self):
        session = make_session(make_profile(recording_block=None))

        session._observe_recording_state(_recording_packet(0))

        assert session.is_recording is None


class TestWaitWhileRecording:
    @pytest.mark.asyncio
    async def test_returns_true_when_timeout_elapses_undisturbed(self):
        session = make_session(make_profile())

        held = await session.wait_while_recording(0.05)

        assert held is True

    @pytest.mark.asyncio
    async def test_returns_false_when_unexpected_stop_is_observed(self):
        session = make_session(make_profile())
        session.is_recording = True

        async def stop_soon():
            await asyncio.sleep(0.01)
            session._observe_recording_state(_recording_packet(0))

        asyncio.create_task(stop_soon())
        held = await session.wait_while_recording(1.0)

        assert held is False
        assert session.last_stop_reason == "unexpected"


class TestRecordStopNoOp:
    @pytest.mark.asyncio
    async def test_no_op_when_already_confirmed_not_recording(self):
        session = make_session(make_profile())
        session.is_recording = False

        await session.record_stop()

        session._controller.write_outgoing_control.assert_not_awaited()
        session._router.wait_for.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_still_sends_when_recording_state_unknown(self):
        session = make_session(make_profile())
        session.is_recording = None
        session._router.wait_for.return_value = (
            MagicMock(category=0x0A, parameter=0x01),
            bytes([0x00]),
        )

        await session.record_stop()

        session._controller.write_outgoing_control.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_still_sends_when_recording_state_is_true(self):
        session = make_session(make_profile())
        session.is_recording = True
        session._router.wait_for.return_value = (
            MagicMock(category=0x0A, parameter=0x01),
            bytes([0x00]),
        )

        await session.record_stop()

        session._controller.write_outgoing_control.assert_awaited_once()


class TestObserveWriteMargin:
    """CANDIDATE signal (see docs/recording.md): a storage notification
    observed to precede a camera-initiated stop on a known-slow SD card.
    `_observe_write_margin` tracks it; `_observe_recording_state` only
    attaches it to an unexpected stop when it fired recently enough, and
    never claims it without direct supporting evidence for that stop."""

    def test_low_margin_alone_does_not_touch_stop_fields(self):
        session = make_session(make_profile())

        session._observe_write_margin(_storage_packet(-2))

        assert session._last_low_margin_at is not None
        assert session.last_stop_reason is None
        assert session.last_stop_signal is None

    def test_nominal_reading_never_sets_low_margin_timestamp(self):
        session = make_session(make_profile())

        session._observe_write_margin(_storage_packet(1))

        assert session._last_low_margin_at is None

    def test_ignores_malformed_data(self):
        session = make_session(make_profile())

        session._observe_write_margin(bytearray([0x01, 0x02]))

        assert session._last_low_margin_at is None

    def test_no_op_without_a_storage_command_block(self):
        session = make_session(make_profile(storage_block=None))

        session._observe_write_margin(_storage_packet(-2))

        assert session._last_low_margin_at is None

    def test_unexpected_stop_within_window_sets_low_write_margin_signal(self):
        session = make_session(make_profile())
        session.is_recording = True

        session._observe_write_margin(_storage_packet(-2))
        session._observe_recording_state(_recording_packet(0))

        assert session.last_stop_reason == "unexpected"
        assert session.last_stop_signal == "low_write_margin"

    def test_unexpected_stop_with_no_prior_warning_keeps_signal_none(self):
        session = make_session(make_profile())
        session.is_recording = True

        session._observe_recording_state(_recording_packet(0))

        assert session.last_stop_reason == "unexpected"
        assert session.last_stop_signal is None

    def test_unexpected_stop_outside_window_keeps_signal_none(self):
        session = make_session(make_profile())
        session.is_recording = True
        session.write_margin_window_s = 2.0
        session._last_low_margin_at = time.monotonic() - 5.0  # older than the window

        session._observe_recording_state(_recording_packet(0))

        assert session.last_stop_reason == "unexpected"
        assert session.last_stop_signal is None

    def test_requested_stop_never_sets_low_write_margin_signal(self):
        """A confirmed record_stop() must not pick up a stale low-margin
        reading — last_stop_signal only ever applies to unexpected stops."""
        session = make_session(make_profile())
        session.is_recording = True
        session._pending_command = True

        session._observe_write_margin(_storage_packet(-2))
        session._observe_recording_state(_recording_packet(0))

        assert session.last_stop_reason is None  # unchanged: _pending_command suppressed it
        assert session.last_stop_signal is None

    def test_low_margin_timestamp_resets_on_fresh_recording_start(self):
        session = make_session(make_profile())
        session.is_recording = False
        session._last_low_margin_at = time.monotonic()

        session._observe_recording_state(_recording_packet(2))

        assert session.is_recording is True
        assert session._last_low_margin_at is None


def make_settings_profile(**overrides) -> CameraProfile:
    """A real CameraProfile carrying the three CANDIDATE settings command
    blocks plus the codecs/resolutions/fps_modes lookup tables they consume
    (mirroring POCKET_6K_G2_v7.9.json — see docs/settings.md)."""
    raw = {
        "_meta": {"model": "Pocket 6K G2", "ble_name": "A:TEST"},
        "commands": {
            "codec_quality": {
                "category": 0x0A,
                "parameter": 0x00,
                "data_type": "INT8",
                "reserved": 0x00,
            },
            "video_format": {
                "category": 0x01,
                "parameter": 0x00,
                "data_type": "INT8",
                "reserved": 0x01,
            },
            "recording_format": {
                "category": 0x01,
                "parameter": 0x09,
                "data_type": "INT16_ARRAY",
                "reserved": 0x01,
            },
        },
        "codecs": {
            "BRAW": {"id": 3, "variants": {"Q0": 0, "5:1": 3}},
            "ProRes": {"id": 2, "variants": {"HQ": 0}},
        },
        "resolutions": {
            "4K DCI": {
                "width": 4096,
                "height": 2160,
                "codecs": ["BRAW", "ProRes"],
                "dimension_enums": {"BRAW": 8},
            },
            "HD": {
                "width": 1920,
                "height": 1080,
                "codecs": ["ProRes"],
                "dimension_enums": {"ProRes": 3},
            },
        },
        "fps_modes": {
            "25": {"fps_int": 25, "m_rate": 0, "frame_flags": 16},
            "23.98": {"fps_int": 24, "m_rate": 1, "frame_flags": 19},
        },
    }
    raw.update(overrides)
    return CameraProfile._from_raw(MODEL_KEY, FIRMWARE, raw)


def _recording_format_payload(fps: int, sensor_fps: int, width: int, height: int, flags: int):
    import struct

    return struct.pack("<5h", fps, sensor_fps, width, height, flags)


def _recording_format_packet(
    fps: int, sensor_fps: int, width: int, height: int, flags: int
) -> bytearray:
    """A recording_format-category CAMERA_REPORT, matching
    `make_settings_profile`'s default block (category=0x01/parameter=0x09/
    INT16_ARRAY) — mirrors the real capture's five little-endian int16
    elements. This is also the exact (category, parameter) set_video_format's
    mode-notify channel reports on."""
    header = CommandHeader(
        destination=0xFF,
        command_id=0x00,
        reserved=0x01,
        category=0x01,
        parameter=0x09,
        data_type=DataType.INT16_ARRAY,
        operation=Operation.CAMERA_REPORT,
    )
    payload = _recording_format_payload(fps, sensor_fps, width, height, flags)
    return bytearray(encode_packet(header, payload))


class TestObserveCodecQuality:
    """`last_known_codec_variant` is notification-derived only (design
    principle 4), like is_recording — never set from "we sent a command".
    set_codec_quality's no-op guard depends on it being accurate."""

    def test_updates_from_any_codec_quality_report(self):
        session = make_session(make_settings_profile())

        session._observe_codec_quality(_codec_quality_packet(3, 3))

        assert session.last_known_codec_variant == (3, 3)

    def test_later_report_overwrites_earlier_one(self):
        session = make_session(make_settings_profile())
        session._observe_codec_quality(_codec_quality_packet(3, 3))

        session._observe_codec_quality(_codec_quality_packet(2, 0))

        assert session.last_known_codec_variant == (2, 0)

    def test_ignores_malformed_data(self):
        session = make_session(make_settings_profile())

        session._observe_codec_quality(bytearray([0x01, 0x02]))

        assert session.last_known_codec_variant is None

    def test_ignores_unrelated_notification(self):
        session = make_session(make_settings_profile())

        session._observe_codec_quality(_recording_packet(2))

        assert session.last_known_codec_variant is None

    def test_no_op_without_a_codec_quality_command_block(self):
        session = make_session(make_profile())  # recording-only profile

        session._observe_codec_quality(_codec_quality_packet(3, 3))

        assert session.last_known_codec_variant is None

    def test_wired_through_handle_incoming(self):
        session = make_session(make_settings_profile())

        session._handle_incoming(MagicMock(), _codec_quality_packet(2, 1))

        assert session.last_known_codec_variant == (2, 1)


class TestObserveRecordingFormat:
    """`last_known_recording_format` is notification-derived only (design
    principle 4), like is_recording and last_known_codec_variant. Both
    set_recording_format's and set_video_format's no-op guards depend on it
    being accurate — and since this is the exact (category, parameter)
    set_video_format's mode-notify channel reports on, a video_format
    write's own confirmation updates it too."""

    def test_updates_from_any_recording_format_report(self):
        session = make_session(make_settings_profile())

        session._observe_recording_format(_recording_format_packet(25, 25, 4096, 2160, 16))

        assert session.last_known_recording_format == (25, 4096, 2160)

    def test_later_report_overwrites_earlier_one(self):
        session = make_session(make_settings_profile())
        session._observe_recording_format(_recording_format_packet(25, 25, 4096, 2160, 16))

        session._observe_recording_format(_recording_format_packet(24, 24, 1920, 1080, 19))

        assert session.last_known_recording_format == (24, 1920, 1080)

    def test_ignores_malformed_data(self):
        session = make_session(make_settings_profile())

        session._observe_recording_format(bytearray([0x01, 0x02]))

        assert session.last_known_recording_format is None

    def test_ignores_unrelated_notification(self):
        session = make_session(make_settings_profile())

        session._observe_recording_format(_codec_quality_packet(3, 3))

        assert session.last_known_recording_format is None

    def test_no_op_without_a_recording_format_command_block(self):
        session = make_session(make_profile())  # recording-only profile

        session._observe_recording_format(_recording_format_packet(25, 25, 4096, 2160, 16))

        assert session.last_known_recording_format is None

    def test_wired_through_handle_incoming(self):
        session = make_session(make_settings_profile())

        session._handle_incoming(MagicMock(), _recording_format_packet(25, 25, 4096, 2160, 16))

        assert session.last_known_recording_format == (25, 4096, 2160)


class TestSetCodecQuality:
    @pytest.mark.asyncio
    async def test_succeeds_on_matching_echo(self):
        session = make_session(make_settings_profile())
        session._router.wait_for.return_value = (
            MagicMock(category=0x0A, parameter=0x00),
            bytes([3, 3]),
        )

        await session.set_codec_quality("BRAW", "5:1")

        session._router.arm.assert_called_once_with(0x0A, 0x00)
        session._controller.write_outgoing_control.assert_awaited_once()
        session._router.wait_for.assert_awaited_once_with(0x0A, 0x00, timeout=2.0)

    @pytest.mark.asyncio
    async def test_sends_documented_packet_bytes(self):
        session = make_session(make_settings_profile())
        session._router.wait_for.return_value = (MagicMock(), bytes([2, 0]))

        await session.set_codec_quality("ProRes", "HQ")

        (sent,) = session._controller.write_outgoing_control.await_args.args
        assert sent == bytes([0xFF, 0x06, 0x00, 0x00, 0x0A, 0x00, 0x01, 0x00, 0x02, 0x00])

    @pytest.mark.asyncio
    async def test_raises_when_no_echo_arrives(self):
        session = make_session(make_settings_profile())
        session._router.wait_for.return_value = None

        with pytest.raises(BMDVerificationError, match="no echo received"):
            await session.set_codec_quality("BRAW", "5:1")

    @pytest.mark.asyncio
    async def test_raises_when_echo_reports_other_codec(self):
        """The documented codec_quality limitation: the camera keeps its
        active codec family, so the echo disagrees with the request."""
        session = make_session(make_settings_profile())
        session._router.wait_for.return_value = (MagicMock(), bytes([3, 3]))

        with pytest.raises(BMDVerificationError, match="set_video_format"):
            await session.set_codec_quality("ProRes", "HQ")

    @pytest.mark.asyncio
    async def test_is_a_noop_when_already_at_the_target(self):
        """Real-hardware regression (docs/settings.md §11): a video_format
        switch resets the family's quality to a remembered value, and
        requesting that same value again used to raise a spurious
        BMDVerificationError. last_known_codec_variant (notification-
        derived) lets this be recognized as already-satisfied instead."""
        session = make_session(make_settings_profile())
        session.last_known_codec_variant = (3, 3)  # BRAW, 5:1 — already known

        await session.set_codec_quality("BRAW", "5:1")

        session._controller.write_outgoing_control.assert_not_awaited()
        session._router.arm.assert_not_called()

    @pytest.mark.asyncio
    async def test_writes_normally_when_last_known_variant_differs(self):
        session = make_session(make_settings_profile())
        session.last_known_codec_variant = (2, 0)  # ProRes, HQ
        session._router.wait_for.return_value = (MagicMock(), bytes([3, 3]))

        await session.set_codec_quality("BRAW", "5:1")

        session._controller.write_outgoing_control.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_writes_normally_when_nothing_known_yet(self):
        session = make_session(make_settings_profile())
        assert session.last_known_codec_variant is None
        session._router.wait_for.return_value = (MagicMock(), bytes([3, 3]))

        await session.set_codec_quality("BRAW", "5:1")

        session._controller.write_outgoing_control.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_before_writing_on_unknown_variant(self):
        session = make_session(make_settings_profile())

        with pytest.raises(ValueError, match="no variant '12:1'"):
            await session.set_codec_quality("BRAW", "12:1")

        session._controller.write_outgoing_control.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_before_writing_when_block_missing(self):
        session = make_session(make_profile())  # recording-only profile

        with pytest.raises(ValueError, match="no 'codec_quality' command block"):
            await session.set_codec_quality("BRAW", "5:1")

        session._controller.write_outgoing_control.assert_not_awaited()


class TestSetVideoFormat:
    @pytest.mark.asyncio
    async def test_succeeds_on_own_channel_echo(self):
        session = make_session(make_settings_profile())

        async def wait_for(category, parameter, timeout):
            if (category, parameter) == (0x01, 0x00):
                return MagicMock(category=0x01, parameter=0x00), bytes([25, 0, 8, 0, 0])
            return None

        session._router.wait_for = AsyncMock(side_effect=wait_for)

        await session.set_video_format("4K DCI", "BRAW", "25")

        armed = {call.args for call in session._router.arm.call_args_list}
        assert armed == {(0x01, 0x00), (0x01, 0x09), (0x0A, 0x00)}
        (sent,) = session._controller.write_outgoing_control.await_args.args
        assert sent == bytes(
            [0xFF, 0x09, 0x00, 0x01, 0x01, 0x00, 0x01, 0x00, 0x19, 0x00, 0x08, 0x00, 0x00]
        )

    @pytest.mark.asyncio
    async def test_succeeds_on_mode_notify_echo(self):
        """The echo channel is unconfirmed — a report on the
        recording_format coordinates (1/9) also verifies the write."""
        session = make_session(make_settings_profile())

        async def wait_for(category, parameter, timeout):
            if (category, parameter) == (0x01, 0x09):
                return (
                    MagicMock(category=0x01, parameter=0x09),
                    _recording_format_payload(25, 25, 4096, 2160, 16),
                )
            return None

        session._router.wait_for = AsyncMock(side_effect=wait_for)

        await session.set_video_format("4K DCI", "BRAW", "25")

    @pytest.mark.asyncio
    async def test_succeeds_on_codec_quality_channel_echo(self):
        """Real-hardware regression (docs/settings.md §10): a codec-only
        switch (same resolution/fps, different family) leaves the
        mode-notify payload byte-identical to what NotificationRouter
        already saw, so it gets filtered as a stale duplicate — the
        codec_quality channel (10/0) is what actually confirms the write
        in that case."""
        session = make_session(make_settings_profile())

        async def wait_for(category, parameter, timeout):
            if (category, parameter) == (0x0A, 0x00):
                return MagicMock(category=0x0A, parameter=0x00), bytes([3, 3])  # BRAW, 5:1
            return None

        session._router.wait_for = AsyncMock(side_effect=wait_for)

        await session.set_video_format("4K DCI", "BRAW", "25")

    @pytest.mark.asyncio
    async def test_raises_when_codec_quality_channel_reports_wrong_codec(self):
        session = make_session(make_settings_profile())

        async def wait_for(category, parameter, timeout):
            if (category, parameter) == (0x0A, 0x00):
                return MagicMock(category=0x0A, parameter=0x00), bytes([2, 0])  # ProRes, HQ
            return None

        session._router.wait_for = AsyncMock(side_effect=wait_for)

        with pytest.raises(BMDVerificationError, match="codec_quality-channel reported"):
            await session.set_video_format("4K DCI", "BRAW", "25")

    @pytest.mark.asyncio
    async def test_does_not_arm_codec_quality_channel_when_profile_lacks_it(self):
        profile = make_settings_profile()
        profile.commands.pop("codec_quality")
        session = make_session(profile)

        async def wait_for(category, parameter, timeout):
            if (category, parameter) == (0x01, 0x09):
                return (
                    MagicMock(category=0x01, parameter=0x09),
                    _recording_format_payload(25, 25, 4096, 2160, 16),
                )
            return None

        session._router.wait_for = AsyncMock(side_effect=wait_for)

        await session.set_video_format("4K DCI", "BRAW", "25")

        armed = {call.args for call in session._router.arm.call_args_list}
        assert armed == {(0x01, 0x00), (0x01, 0x09)}

    @pytest.mark.asyncio
    async def test_raises_when_no_channel_echoes(self):
        session = make_session(make_settings_profile())
        session._router.wait_for.return_value = None

        with pytest.raises(BMDVerificationError, match="no echo received"):
            await session.set_video_format("4K DCI", "BRAW", "25")

    @pytest.mark.asyncio
    async def test_raises_when_mode_notify_disagrees(self):
        session = make_session(make_settings_profile())

        async def wait_for(category, parameter, timeout):
            if (category, parameter) == (0x01, 0x09):
                return (
                    MagicMock(category=0x01, parameter=0x09),
                    _recording_format_payload(25, 25, 1920, 1080, 16),  # wrong resolution
                )
            return None

        session._router.wait_for = AsyncMock(side_effect=wait_for)

        with pytest.raises(BMDVerificationError, match="mode-notify reported"):
            await session.set_video_format("4K DCI", "BRAW", "25")

    @pytest.mark.asyncio
    async def test_raises_unsupported_before_writing(self):
        from bmd_ble.exceptions import BMDUnsupportedError

        session = make_session(make_settings_profile())

        with pytest.raises(BMDUnsupportedError, match="does not offer codec 'BRAW' at 'HD'"):
            await session.set_video_format("HD", "BRAW", "25")

        session._controller.write_outgoing_control.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_before_writing_when_dimension_enum_uncaptured(self):
        session = make_session(make_settings_profile())

        with pytest.raises(ValueError, match="dimension_enum for '4K DCI' under 'ProRes'"):
            await session.set_video_format("4K DCI", "ProRes", "25")

        session._controller.write_outgoing_control.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_is_a_noop_when_family_and_format_already_known(self):
        """Real-hardware regression (docs/settings.md §14, 7/7 --repeat 2
        runs): requesting the (resolution, codec, fps) the camera is
        already in produces no echo on any channel, mirroring
        set_codec_quality's §11 finding. The guard reuses
        last_known_codec_variant (codec family) and
        last_known_recording_format (resolution+fps) rather than tracking a
        third field."""
        session = make_session(make_settings_profile())
        session.last_known_codec_variant = (3, 3)  # BRAW, 5:1
        session.last_known_recording_format = (25, 4096, 2160)  # 4K DCI, 25fps

        await session.set_video_format("4K DCI", "BRAW", "25")

        session._controller.write_outgoing_control.assert_not_awaited()
        session._router.arm.assert_not_called()

    @pytest.mark.asyncio
    async def test_writes_normally_when_only_codec_family_matches(self):
        session = make_session(make_settings_profile())
        session.last_known_codec_variant = (3, 3)  # BRAW — matches
        session.last_known_recording_format = (25, 1920, 1080)  # wrong resolution

        async def wait_for(category, parameter, timeout):
            if (category, parameter) == (0x01, 0x00):
                return MagicMock(category=0x01, parameter=0x00), bytes([25, 0, 8, 0, 0])
            return None

        session._router.wait_for = AsyncMock(side_effect=wait_for)

        await session.set_video_format("4K DCI", "BRAW", "25")

        session._controller.write_outgoing_control.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_writes_normally_when_only_format_matches(self):
        session = make_session(make_settings_profile())
        session.last_known_codec_variant = (2, 0)  # ProRes — wrong family
        session.last_known_recording_format = (25, 4096, 2160)  # matches

        async def wait_for(category, parameter, timeout):
            if (category, parameter) == (0x01, 0x00):
                return MagicMock(category=0x01, parameter=0x00), bytes([25, 0, 8, 0, 0])
            return None

        session._router.wait_for = AsyncMock(side_effect=wait_for)

        await session.set_video_format("4K DCI", "BRAW", "25")

        session._controller.write_outgoing_control.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_writes_normally_when_nothing_known_yet(self):
        session = make_session(make_settings_profile())
        assert session.last_known_codec_variant is None
        assert session.last_known_recording_format is None

        async def wait_for(category, parameter, timeout):
            if (category, parameter) == (0x01, 0x00):
                return MagicMock(category=0x01, parameter=0x00), bytes([25, 0, 8, 0, 0])
            return None

        session._router.wait_for = AsyncMock(side_effect=wait_for)

        await session.set_video_format("4K DCI", "BRAW", "25")

        session._controller.write_outgoing_control.assert_awaited_once()


class TestSetRecordingFormat:
    @pytest.mark.asyncio
    async def test_succeeds_on_matching_echo(self):
        session = make_session(make_settings_profile())
        session._router.wait_for.return_value = (
            MagicMock(category=0x01, parameter=0x09),
            _recording_format_payload(25, 25, 4096, 2160, 16),
        )

        await session.set_recording_format("4K DCI", "25")

        session._router.arm.assert_called_once_with(0x01, 0x09)
        (sent,) = session._controller.write_outgoing_control.await_args.args
        assert sent == bytes.fromhex("FF 0E 00 01 01 09 82 00 19 00 19 00 00 10 70 08 10 00")

    @pytest.mark.asyncio
    async def test_ntsc_mode_uses_drop_frame_flags(self):
        session = make_session(make_settings_profile())
        session._router.wait_for.return_value = (
            MagicMock(),
            _recording_format_payload(24, 24, 4096, 2160, 19),
        )

        await session.set_recording_format("4K DCI", "23.98")

        (sent,) = session._controller.write_outgoing_control.await_args.args
        assert sent[8:] == _recording_format_payload(24, 24, 4096, 2160, 19)

    @pytest.mark.asyncio
    async def test_raises_when_no_echo_arrives(self):
        session = make_session(make_settings_profile())
        session._router.wait_for.return_value = None

        with pytest.raises(BMDVerificationError, match="no echo received"):
            await session.set_recording_format("4K DCI", "25")

    @pytest.mark.asyncio
    async def test_raises_when_echo_reports_wrong_resolution(self):
        session = make_session(make_settings_profile())
        session._router.wait_for.return_value = (
            MagicMock(),
            _recording_format_payload(25, 25, 1920, 1080, 16),
        )

        with pytest.raises(BMDVerificationError, match="echo reported"):
            await session.set_recording_format("4K DCI", "25")

    @pytest.mark.asyncio
    async def test_is_a_noop_when_already_at_the_target(self):
        """Real-hardware regression (docs/settings.md §14, 5/5 --repeat 2
        runs): requesting the (resolution, fps) the camera is already at
        produces no echo at all, mirroring set_codec_quality's §11 finding.
        last_known_recording_format (notification-derived) lets this be
        recognized as already-satisfied instead of raising."""
        session = make_session(make_settings_profile())
        session.last_known_recording_format = (25, 4096, 2160)  # 4K DCI, 25fps

        await session.set_recording_format("4K DCI", "25")

        session._controller.write_outgoing_control.assert_not_awaited()
        session._router.arm.assert_not_called()

    @pytest.mark.asyncio
    async def test_writes_normally_when_last_known_format_differs(self):
        session = make_session(make_settings_profile())
        session.last_known_recording_format = (25, 1920, 1080)  # HD, 25fps
        session._router.wait_for.return_value = (
            MagicMock(),
            _recording_format_payload(25, 25, 4096, 2160, 16),
        )

        await session.set_recording_format("4K DCI", "25")

        session._controller.write_outgoing_control.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_writes_normally_when_nothing_known_yet(self):
        session = make_session(make_settings_profile())
        assert session.last_known_recording_format is None
        session._router.wait_for.return_value = (
            MagicMock(),
            _recording_format_payload(25, 25, 4096, 2160, 16),
        )

        await session.set_recording_format("4K DCI", "25")

        session._controller.write_outgoing_control.assert_awaited_once()


class TestClosestReachableResolution:
    """Tests for the private proxy-selection helper set_camera_format uses."""

    def test_picks_closest_available_resolution(self):
        # 4K DCI has no ProRes dimension_enum in the fixture; HD is the only
        # other ProRes-enabled resolution, so it's the (only, hence closest)
        # candidate.
        session = make_session(make_settings_profile())

        assert session._closest_reachable_resolution("4K DCI", "ProRes") == "HD"

    def test_returns_target_itself_when_it_already_has_the_enum(self):
        session = make_session(make_settings_profile())

        assert session._closest_reachable_resolution("4K DCI", "BRAW") == "4K DCI"

    def test_raises_when_no_resolution_offers_the_codec(self):
        session = make_session(make_settings_profile())

        with pytest.raises(ValueError, match="No resolution with a known dimension_enum"):
            session._closest_reachable_resolution("4K DCI", "H265")

    def test_raises_naming_unknown_target_resolution(self):
        session = make_session(make_settings_profile())

        with pytest.raises(ValueError, match="no resolution 'UHD'"):
            session._closest_reachable_resolution("UHD", "ProRes")


class TestSetCameraFormat:
    """Tests for the (codec, variant, resolution, fps) orchestration method.

    These mock CameraSession's own set_video_format/set_codec_quality/
    set_recording_format rather than the router+echo mechanics — each of
    those is already thoroughly tested by its own TestSet* class above;
    set_camera_format's job is purely to sequence them with the right
    arguments (including the proxy-resolution substitution), which is what
    these tests verify.
    """

    @pytest.mark.asyncio
    async def test_direct_path_uses_target_resolution_for_video_format(self):
        session = make_session(make_settings_profile())
        session.set_video_format = AsyncMock()
        session.set_codec_quality = AsyncMock()
        session.set_recording_format = AsyncMock()

        await session.set_camera_format("BRAW", "5:1", "4K DCI", "25")

        session.set_video_format.assert_awaited_once_with("4K DCI", "BRAW", "25")
        session.set_codec_quality.assert_awaited_once_with("BRAW", "5:1")
        session.set_recording_format.assert_awaited_once_with("4K DCI", "25")

    @pytest.mark.asyncio
    async def test_proxy_path_switches_video_format_through_closest_resolution_first(self):
        session = make_session(make_settings_profile())
        session.set_video_format = AsyncMock()
        session.set_codec_quality = AsyncMock()
        session.set_recording_format = AsyncMock()

        # 4K DCI has no ProRes dimension_enum in the fixture -- video_format
        # must go through HD (the only ProRes-enabled resolution) first,
        # while set_codec_quality and the final set_recording_format still
        # target the caller's real request.
        await session.set_camera_format("ProRes", "HQ", "4K DCI", "25")

        session.set_video_format.assert_awaited_once_with("HD", "ProRes", "25")
        session.set_codec_quality.assert_awaited_once_with("ProRes", "HQ")
        session.set_recording_format.assert_awaited_once_with("4K DCI", "25")

    @pytest.mark.asyncio
    async def test_steps_run_in_order(self):
        session = make_session(make_settings_profile())
        order: list[str] = []
        session.set_video_format = AsyncMock(side_effect=lambda *a: order.append("video_format"))
        session.set_codec_quality = AsyncMock(side_effect=lambda *a: order.append("codec_quality"))
        session.set_recording_format = AsyncMock(
            side_effect=lambda *a: order.append("recording_format")
        )

        await session.set_camera_format("BRAW", "5:1", "4K DCI", "25")

        assert order == ["video_format", "codec_quality", "recording_format"]

    @pytest.mark.asyncio
    async def test_propagates_verification_error_from_video_format_step_and_stops(self):
        session = make_session(make_settings_profile())
        session.set_video_format = AsyncMock(side_effect=BMDVerificationError("boom"))
        session.set_codec_quality = AsyncMock()
        session.set_recording_format = AsyncMock()

        with pytest.raises(BMDVerificationError, match="boom"):
            await session.set_camera_format("BRAW", "5:1", "4K DCI", "25")

        session.set_codec_quality.assert_not_awaited()
        session.set_recording_format.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_propagates_verification_error_from_codec_quality_step_and_stops(self):
        session = make_session(make_settings_profile())
        session.set_video_format = AsyncMock()
        session.set_codec_quality = AsyncMock(side_effect=BMDVerificationError("no echo"))
        session.set_recording_format = AsyncMock()

        with pytest.raises(BMDVerificationError, match="no echo"):
            await session.set_camera_format("BRAW", "5:1", "4K DCI", "25")

        session.set_recording_format.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_before_any_call_on_unknown_resolution(self):
        session = make_session(make_settings_profile())
        session.set_video_format = AsyncMock()

        with pytest.raises(ValueError, match="no resolution 'UHD'"):
            await session.set_camera_format("ProRes", "HQ", "UHD", "25")

        session.set_video_format.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_before_any_call_on_unknown_variant(self):
        session = make_session(make_settings_profile())
        session.set_video_format = AsyncMock()

        with pytest.raises(ValueError, match="no variant '12:1'"):
            await session.set_camera_format("BRAW", "12:1", "4K DCI", "25")

        session.set_video_format.assert_not_awaited()
