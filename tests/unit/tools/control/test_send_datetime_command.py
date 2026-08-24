"""Unit tests for tools/control/send_datetime_command.py — pure BCD-packing
and command-building logic only.

No BLE, no input(), no hardware — matches tests/unit/'s "no hardware, full
mocking" rule and tests/unit/tools/common/test_capture.py's sys.path pattern
for importing a standalone (non-package) tools/ script. This tool's payload
encoding is an unconfirmed hypothesis (see its own module docstring) — these
tests verify the arithmetic is self-consistent, not that it matches real
camera behavior, which no capture has confirmed yet.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "control"))

import send_datetime_command as sdc  # noqa: E402

from bmd_camera.ble.protocol.codec import decode_packet  # noqa: E402
from bmd_camera.ble.protocol.types import DataType  # noqa: E402


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        parameter=None,
        minutes=None,
        date=None,
        time=None,
        raw_elements=None,
        when=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestPackBcdDate:
    def test_packs_year_month_day_as_hex_digits(self):
        assert sdc._pack_bcd_date(2026, 8, 24) == 0x20260824

    def test_pads_single_digit_month_and_day(self):
        assert sdc._pack_bcd_date(2026, 1, 5) == 0x20260105


class TestPackBcdTime:
    def test_packs_hour_minute_second_with_trailing_zero_digits(self):
        assert sdc._pack_bcd_time(11, 40, 0) == 0x11400000

    def test_pads_single_digit_components(self):
        assert sdc._pack_bcd_time(1, 2, 3) == 0x01020300


class TestBuildCommand:
    def test_timezone_requires_minutes(self):
        with pytest.raises(SystemExit, match="--minutes"):
            sdc.build_command(_args(parameter="timezone"))

    def test_timezone_encodes_plain_int32_assign(self):
        label, command = sdc.build_command(_args(parameter="timezone", minutes=330))

        header, payload = decode_packet(command)
        assert header.category == sdc.CATEGORY_CONFIGURATION
        assert header.parameter == sdc.PARAMETER_TIMEZONE
        assert header.data_type == DataType.INT32
        assert int.from_bytes(payload, byteorder="little", signed=True) == 330
        assert "330" in label

    def test_timezone_encodes_negative_offset(self):
        _, command = sdc.build_command(_args(parameter="timezone", minutes=-300))

        _, payload = decode_packet(command)
        assert int.from_bytes(payload, byteorder="little", signed=True) == -300

    def test_rtc_with_explicit_when_uses_bcd_hypothesis(self):
        when = datetime(2026, 8, 24, 11, 40, 0)
        label, command = sdc.build_command(_args(parameter="rtc", when=when))

        header, payload = decode_packet(command)
        assert header.category == sdc.CATEGORY_CONFIGURATION
        assert header.parameter == sdc.PARAMETER_RTC
        assert header.data_type == DataType.INT32
        time_value = int.from_bytes(payload[0:4], byteorder="little")
        date_value = int.from_bytes(payload[4:8], byteorder="little")
        assert time_value == 0x11400000
        assert date_value == 0x20260824
        assert "BCD hypothesis" in label

    def test_rtc_raw_elements_bypasses_bcd_packing(self):
        label, command = sdc.build_command(
            _args(parameter="rtc", raw_elements=(0xDEADBEEF, 0x12345678))
        )

        header, payload = decode_packet(command)
        assert header.category == sdc.CATEGORY_CONFIGURATION
        assert header.parameter == sdc.PARAMETER_RTC
        time_value = int.from_bytes(payload[0:4], byteorder="little")
        date_value = int.from_bytes(payload[4:8], byteorder="little")
        assert time_value == 0xDEADBEEF
        assert date_value == 0x12345678
        assert "raw_elements" in label

    def test_language_not_implemented_raises_system_exit(self):
        with pytest.raises(SystemExit, match="not implemented"):
            sdc.build_command(_args(parameter="language"))


class TestParseWhen:
    def test_explicit_date_and_time_both_used(self):
        result = sdc._parse_when(_args(date="2026-08-24", time="11:40:00"))

        assert result == datetime(2026, 8, 24, 11, 40, 0)

    def test_missing_date_defaults_to_today(self):
        result = sdc._parse_when(_args(date=None, time="11:40:00"))

        today = datetime.now()
        assert (result.year, result.month, result.day) == (today.year, today.month, today.day)
        assert (result.hour, result.minute, result.second) == (11, 40, 0)
