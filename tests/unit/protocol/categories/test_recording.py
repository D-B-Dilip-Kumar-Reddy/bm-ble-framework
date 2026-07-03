"""Unit tests for :mod:`bmd_ble.protocol.categories.recording`.

Category/parameter values used here are arbitrary placeholders for testing
the encode/decode logic only — they are not sniffer-verified BMD protocol
values and must never be copied into a camera profile JSON.
"""

import pytest

from bmd_ble.protocol.categories.recording import (
    decode_recording_state,
    encode_record_start,
    encode_record_stop,
    is_recording_state_echo,
)
from bmd_ble.protocol.codec import Operation, decode_packet
from bmd_ble.protocol.types import DataType

CATEGORY = 0x0A
PARAMETER = 0x01


class TestEncodeRecordStart:
    def test_encode_record_start_sets_true_payload(self):
        packet = encode_record_start(
            category=CATEGORY, parameter=PARAMETER, data_type=DataType.BOOL
        )

        header, payload = decode_packet(packet)

        assert header.category == CATEGORY
        assert header.parameter == PARAMETER
        assert header.operation == Operation.ASSIGN
        assert payload == b"\x01"


class TestEncodeRecordStop:
    def test_encode_record_stop_sets_false_payload(self):
        packet = encode_record_stop(category=CATEGORY, parameter=PARAMETER, data_type=DataType.BOOL)

        header, payload = decode_packet(packet)

        assert header.category == CATEGORY
        assert header.parameter == PARAMETER
        assert header.operation == Operation.ASSIGN
        assert payload == b"\x00"


class TestEncodeRecordingStateUnsupportedType:
    def test_encode_record_start_raises_for_string_type(self):
        with pytest.raises(ValueError, match="Unsupported data type"):
            encode_record_start(category=CATEGORY, parameter=PARAMETER, data_type=DataType.STRING)


class TestIsRecordingStateEcho:
    def test_matches_expected_category_and_parameter(self):
        packet = encode_record_start(
            category=CATEGORY, parameter=PARAMETER, data_type=DataType.BOOL
        )
        header, _ = decode_packet(packet)

        assert is_recording_state_echo(header, category=CATEGORY, parameter=PARAMETER)

    def test_does_not_match_other_category(self):
        packet = encode_record_start(
            category=CATEGORY, parameter=PARAMETER, data_type=DataType.BOOL
        )
        header, _ = decode_packet(packet)

        assert not is_recording_state_echo(header, category=0x0B, parameter=PARAMETER)


class TestDecodeRecordingState:
    def test_decodes_true_payload(self):
        assert decode_recording_state(b"\x01", DataType.BOOL) is True

    def test_decodes_false_payload(self):
        assert decode_recording_state(b"\x00", DataType.BOOL) is False

    def test_raises_on_wrong_payload_width(self):
        with pytest.raises(ValueError, match="Expected 1-byte payload"):
            decode_recording_state(b"\x00\x00", DataType.BOOL)

    def test_raises_for_unsupported_data_type(self):
        with pytest.raises(ValueError, match="Unsupported data type"):
            decode_recording_state(b"", DataType.STRING)
