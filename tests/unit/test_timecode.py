"""Unit tests for :mod:`bmd_ble.timecode`."""

import pytest

from bmd_ble.protocol.codec import CommandHeader, Operation, encode_packet
from bmd_ble.protocol.types import DataType
from bmd_ble.timecode import (
    TIMECODE_CATEGORY,
    TIMECODE_PARAMETER,
    Timecode,
    decode_timecode,
    duration_seconds,
)


def _bcd(value: int) -> int:
    tens, ones = divmod(value, 10)
    return (tens << 4) | ones


def _timecode_packet(
    *,
    frames: int = 0,
    seconds: int = 0,
    minutes: int = 0,
    hours: int = 0,
    category: int = TIMECODE_CATEGORY,
    parameter: int = TIMECODE_PARAMETER,
    data_type: DataType = DataType.INT32,
    operation: Operation = Operation.ASSIGN,
) -> bytes:
    """Build a real-shaped TIMECODE notification (a full wrapped BMD packet).

    Sniffer-verified byte order: payload is BCD [frames, seconds, minutes,
    hours] — see timecode.py's module docstring.
    """
    header = CommandHeader(
        destination=0xFF,
        command_id=0x00,
        reserved=0xFF,
        category=category,
        parameter=parameter,
        data_type=data_type,
        operation=operation,
    )
    payload = bytes(_bcd(v) for v in (frames, seconds, minutes, hours))
    return encode_packet(header, payload)


class TestDecodeTimecode:
    def test_decodes_a_real_shaped_packet(self):
        """Real capture example (POCKET_6K_G2 v7.9 / POCKET_6K_PRO v8.6):
        payload BCD bytes [frames=23, seconds=0, minutes=0, hours=0]."""
        result = decode_timecode(_timecode_packet(frames=23, seconds=0, minutes=0, hours=0))

        assert result == Timecode(hours=0, minutes=0, seconds=0, frames=23)

    def test_decodes_all_zero(self):
        result = decode_timecode(_timecode_packet())

        assert result == Timecode(hours=0, minutes=0, seconds=0, frames=0)

    def test_decodes_double_digit_fields(self):
        result = decode_timecode(_timecode_packet(hours=23, minutes=59, seconds=39, frames=20))

        assert result == Timecode(hours=23, minutes=59, seconds=39, frames=20)

    def test_raises_on_wrong_category(self):
        packet = _timecode_packet(category=0x0A)

        with pytest.raises(ValueError, match="Not a TIMECODE packet"):
            decode_timecode(packet)

    def test_raises_on_wrong_parameter(self):
        packet = _timecode_packet(parameter=0x01)

        with pytest.raises(ValueError, match="Not a TIMECODE packet"):
            decode_timecode(packet)

    def test_raises_on_unexpected_data_type(self):
        packet = _timecode_packet(data_type=DataType.INT8)

        with pytest.raises(ValueError, match="Unexpected TIMECODE data type"):
            decode_timecode(packet)

    def test_raises_on_malformed_packet(self):
        with pytest.raises(ValueError):
            decode_timecode(bytes.fromhex("091253"))


class TestDurationSeconds:
    def test_same_hour_delta(self):
        start = Timecode(hours=9, minutes=12, seconds=53, frames=10)
        stop = Timecode(hours=9, minutes=13, seconds=0, frames=0)

        assert duration_seconds(start, stop) == 7.0

    def test_crosses_minute_boundary(self):
        start = Timecode(hours=1, minutes=0, seconds=50, frames=0)
        stop = Timecode(hours=1, minutes=1, seconds=5, frames=0)

        assert duration_seconds(start, stop) == 15.0

    def test_crosses_hour_boundary(self):
        start = Timecode(hours=9, minutes=59, seconds=55, frames=0)
        stop = Timecode(hours=10, minutes=0, seconds=5, frames=0)

        assert duration_seconds(start, stop) == 10.0

    def test_frames_is_ignored(self):
        """Frames rollover semantics unconfirmed — duration must not depend on it."""
        start = Timecode(hours=0, minutes=0, seconds=0, frames=23)
        stop = Timecode(hours=0, minutes=0, seconds=1, frames=0)

        assert duration_seconds(start, stop) == 1.0

    def test_raises_when_stop_equals_start(self):
        tc = Timecode(hours=1, minutes=2, seconds=3, frames=4)

        with pytest.raises(ValueError, match="is not after start timecode"):
            duration_seconds(tc, tc)

    def test_raises_when_stop_before_start(self):
        start = Timecode(hours=1, minutes=0, seconds=0, frames=0)
        stop = Timecode(hours=0, minutes=59, seconds=59, frames=0)

        with pytest.raises(ValueError, match="is not after start timecode"):
            duration_seconds(start, stop)
