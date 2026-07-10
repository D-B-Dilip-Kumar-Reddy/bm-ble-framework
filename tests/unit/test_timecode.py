"""Unit tests for :mod:`bmd_ble.timecode`."""

import pytest

from bmd_ble.timecode import Timecode, decode_timecode, duration_seconds


class TestDecodeTimecode:
    def test_decodes_documented_example(self):
        """constants.py's documented example: 09:12:53:10 == 0x09125310."""
        result = decode_timecode(bytes.fromhex("09125310"))

        assert result == Timecode(hours=9, minutes=12, seconds=53, subfield=10)

    def test_decodes_all_zero(self):
        result = decode_timecode(bytes.fromhex("00000000"))

        assert result == Timecode(hours=0, minutes=0, seconds=0, subfield=0)

    def test_decodes_double_digit_fields(self):
        result = decode_timecode(bytes.fromhex("23593929"))

        assert result == Timecode(hours=23, minutes=59, seconds=39, subfield=29)

    def test_raises_on_wrong_length(self):
        with pytest.raises(ValueError, match="Expected a 4-byte TIMECODE value, got 3 bytes"):
            decode_timecode(bytes.fromhex("091253"))


class TestDurationSeconds:
    def test_same_hour_delta(self):
        start = Timecode(hours=9, minutes=12, seconds=53, subfield=10)
        stop = Timecode(hours=9, minutes=13, seconds=0, subfield=0)

        assert duration_seconds(start, stop) == 7.0

    def test_crosses_minute_boundary(self):
        start = Timecode(hours=1, minutes=0, seconds=50, subfield=0)
        stop = Timecode(hours=1, minutes=1, seconds=5, subfield=0)

        assert duration_seconds(start, stop) == 15.0

    def test_crosses_hour_boundary(self):
        start = Timecode(hours=9, minutes=59, seconds=55, subfield=0)
        stop = Timecode(hours=10, minutes=0, seconds=5, subfield=0)

        assert duration_seconds(start, stop) == 10.0

    def test_subfield_is_ignored(self):
        """Subfield semantics unconfirmed — duration must not depend on it."""
        start = Timecode(hours=0, minutes=0, seconds=0, subfield=29)
        stop = Timecode(hours=0, minutes=0, seconds=1, subfield=0)

        assert duration_seconds(start, stop) == 1.0

    def test_raises_when_stop_equals_start(self):
        tc = Timecode(hours=1, minutes=2, seconds=3, subfield=4)

        with pytest.raises(ValueError, match="is not after start timecode"):
            duration_seconds(tc, tc)

    def test_raises_when_stop_before_start(self):
        start = Timecode(hours=1, minutes=0, seconds=0, subfield=0)
        stop = Timecode(hours=0, minutes=59, seconds=59, subfield=0)

        with pytest.raises(ValueError, match="is not after start timecode"):
            duration_seconds(start, stop)
