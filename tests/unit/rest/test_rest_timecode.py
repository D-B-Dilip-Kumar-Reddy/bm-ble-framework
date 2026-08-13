"""Unit tests for :mod:`bmd_camera.rest.timecode`."""

from __future__ import annotations

from bmd_camera.rest.timecode import Timecode, decode_rest_timecode


class TestDecodeRestTimecode:
    def test_decodes_real_captured_value(self):
        """POCKET_6K_G2 v8.6, 2026-08-03 (docs/rest/transport.md):
        {"timecode": 274153986} == 0x10574202 == 10:57:42:02, matching the
        time-of-day the sweep actually ran at."""
        tc = decode_rest_timecode(274153986)

        assert tc == Timecode(hours=10, minutes=57, seconds=42, frames=2)

    def test_decodes_zero(self):
        assert decode_rest_timecode(0) == Timecode(hours=0, minutes=0, seconds=0, frames=0)

    def test_byte_order_is_big_endian_hours_first(self):
        """0x01020304 -> hours=01, minutes=02, seconds=03, frames=04 —
        the opposite field order from the BLE TIMECODE characteristic's
        [frames, seconds, minutes, hours]."""
        tc = decode_rest_timecode(0x01020304)

        assert tc == Timecode(hours=1, minutes=2, seconds=3, frames=4)

    def test_max_bcd_digits_per_byte(self):
        # 0x59 -> 59, the highest valid BCD-pair value for minutes/seconds.
        tc = decode_rest_timecode(0x00595923)

        assert tc == Timecode(hours=0, minutes=59, seconds=59, frames=23)
