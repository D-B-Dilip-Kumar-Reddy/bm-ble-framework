"""Unit tests for tools/control/verify_photo_trigger.py's pure logic —
parse_int_list() and summarize_results(). No BLE, no REST, no input(), no
hardware — matches tests/unit/'s "no hardware, full mocking" rule and
tests/unit/tools/control/test_discover_command.py's sys.path pattern for
importing a standalone (non-package) tools/ script.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "control"))

import verify_photo_trigger as vpt  # noqa: E402


class TestParseIntList:
    def test_parses_hex_and_decimal(self):
        assert vpt.parse_int_list("0x00,0x01", "--reserved") == [0, 1]
        assert vpt.parse_int_list("2,10", "--reserved") == [2, 10]

    def test_ignores_blank_entries(self):
        assert vpt.parse_int_list("0,,1", "--reserved") == [0, 1]

    def test_raises_system_exit_on_bad_input(self):
        with pytest.raises(SystemExit, match="--reserved"):
            vpt.parse_int_list("not-a-number", "--reserved")


class TestSummarizeResults:
    def test_single_confirmed_emits_profile_block(self):
        """The exact ambiguity this tool exists for, resolved: only
        reserved=0x00 is REST-confirmed."""
        text = vpt.summarize_results(0x0A, 0x03, {0x00: True, 0x01: False})

        assert "reserved=0x00: CONFIRMED" in text
        assert "reserved=0x01: not confirmed" in text
        assert "category=0x0A parameter=0x03 VOID reserved=0x00" in text

    def test_multiple_confirmed_flags_genuine_indifference(self):
        """Both candidates REST-confirmed — unlike an operator glance, this
        really is evidence the camera ignores the reserved byte, not an
        unreliable read (docs/ble/photo_capture.md §11.4)."""
        text = vpt.summarize_results(0x0A, 0x03, {0x00: True, 0x01: True})

        assert "genuine evidence the camera ignores the reserved byte" in text
        assert "reserved=0x00" in text  # the lowest of the confirmed set

    def test_none_confirmed_suggests_retry(self):
        text = vpt.summarize_results(0x0A, 0x03, {0x00: False, 0x01: False})

        assert "No candidate was REST-confirmed" in text
        assert "CONFIRMED" not in text  # only "not confirmed" (lowercase) should appear
