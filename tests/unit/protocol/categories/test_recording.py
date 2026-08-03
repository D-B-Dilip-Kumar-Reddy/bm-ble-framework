"""Unit tests for :mod:`bmd_camera.ble.protocol.categories.recording`.

These tests exercise generic encode/decode mechanics, not model-specific
truth. ``CATEGORY``/``PARAMETER`` happen to coincide with the real,
reverse-engineered POCKET_6K_G2 v7.9 values (see
payloads/models/POCKET_6K_G2_v7.9.json and docs/ble/recording.md) but that's
incidental to what's tested here.
"""

import pytest

from bmd_camera.ble.protocol.categories.recording import (
    decode_recording_state,
    encode_record_start,
    encode_record_stop,
    is_recording_state_echo,
)
from bmd_camera.ble.protocol.codec import Operation, decode_packet
from bmd_camera.ble.protocol.types import DataType

CATEGORY = 0x0A
PARAMETER = 0x01
START_VALUE = 2
STOP_VALUE = 0
RESERVED = 1


class TestEncodeRecordStart:
    def test_encode_record_start_sets_given_value_as_payload(self):
        packet = encode_record_start(
            category=CATEGORY,
            parameter=PARAMETER,
            data_type=DataType.INT8,
            value=START_VALUE,
            reserved=RESERVED,
        )

        header, payload = decode_packet(packet)

        assert header.category == CATEGORY
        assert header.parameter == PARAMETER
        assert header.operation == Operation.ASSIGN
        assert header.reserved == RESERVED
        assert payload == b"\x02"

    def test_encode_record_start_matches_real_pocket_6k_g2_capture(self):
        """Byte-for-byte cross-check against a real reverse-engineered command."""
        packet = encode_record_start(
            category=CATEGORY,
            parameter=PARAMETER,
            data_type=DataType.INT8,
            value=START_VALUE,
            reserved=RESERVED,
        )

        assert packet == bytes([0xFF, 0x05, 0x00, 0x01, 0x0A, 0x01, 0x01, 0x00, 0x02])


class TestEncodeRecordStop:
    def test_encode_record_stop_sets_given_value_as_payload(self):
        packet = encode_record_stop(
            category=CATEGORY,
            parameter=PARAMETER,
            data_type=DataType.INT8,
            value=STOP_VALUE,
            reserved=RESERVED,
        )

        header, payload = decode_packet(packet)

        assert header.category == CATEGORY
        assert header.parameter == PARAMETER
        assert header.operation == Operation.ASSIGN
        assert header.reserved == RESERVED
        assert payload == b"\x00"

    def test_encode_record_stop_matches_real_pocket_6k_g2_capture(self):
        """Byte-for-byte cross-check against a real reverse-engineered command."""
        packet = encode_record_stop(
            category=CATEGORY,
            parameter=PARAMETER,
            data_type=DataType.INT8,
            value=STOP_VALUE,
            reserved=RESERVED,
        )

        assert packet == bytes([0xFF, 0x05, 0x00, 0x01, 0x0A, 0x01, 0x01, 0x00, 0x00])


class TestEncodeRecordingStateUnsupportedType:
    def test_encode_record_start_raises_for_string_type(self):
        with pytest.raises(ValueError, match="Unsupported data type"):
            encode_record_start(
                category=CATEGORY,
                parameter=PARAMETER,
                data_type=DataType.STRING,
                value=START_VALUE,
            )


class TestIsRecordingStateEcho:
    def test_matches_expected_category_and_parameter(self):
        packet = encode_record_start(
            category=CATEGORY, parameter=PARAMETER, data_type=DataType.INT8, value=START_VALUE
        )
        header, _ = decode_packet(packet)

        assert is_recording_state_echo(header, category=CATEGORY, parameter=PARAMETER)

    def test_does_not_match_other_category(self):
        packet = encode_record_start(
            category=CATEGORY, parameter=PARAMETER, data_type=DataType.INT8, value=START_VALUE
        )
        header, _ = decode_packet(packet)

        assert not is_recording_state_echo(header, category=0x0B, parameter=PARAMETER)


class TestDecodeRecordingState:
    def test_decodes_nonzero_value_as_recording(self):
        """Real hardware uses 2, not 1, for the "recording" payload value."""
        assert decode_recording_state(b"\x02", DataType.INT8) is True

    def test_decodes_zero_value_as_stopped(self):
        assert decode_recording_state(b"\x00", DataType.INT8) is False

    def test_raises_on_payload_shorter_than_width(self):
        with pytest.raises(ValueError, match="Expected at least 1-byte payload"):
            decode_recording_state(b"", DataType.INT8)

    def test_raises_for_unsupported_data_type(self):
        with pytest.raises(ValueError, match="Unsupported data type"):
            decode_recording_state(b"", DataType.STRING)

    def test_decodes_real_pocket_6k_g2_record_start_echo(self):
        """Real CAMERA_REPORT echo payload: 6 bytes, recording flag leads, rest unexplained."""
        payload = bytes([0x02, 0x00, 0x40, 0x00, 0x01, 0x03])
        assert decode_recording_state(payload, DataType.INT8) is True

    def test_decodes_real_pocket_6k_g2_record_stop_echo(self):
        payload = bytes([0x00, 0x00, 0x40, 0x00, 0x01, 0x03])
        assert decode_recording_state(payload, DataType.INT8) is False
