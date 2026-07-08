"""Unit tests for :mod:`bmd_ble.protocol.codec`.

Covers BMD command packet header encode/decode round-trips and malformed
packet handling.
"""

import pytest

from bmd_ble.protocol.codec import (
    DESTINATION_CAMERA,
    HEADER_LENGTH,
    RESERVED_BYTE,
    CommandHeader,
    Operation,
    decode_packet,
    encode_assign,
    encode_packet,
)
from bmd_ble.protocol.types import DataType


class TestEncodePacket:
    """Tests for ``encode_packet``."""

    def test_encode_packet_with_no_payload(self):
        """A header with an empty payload encodes to exactly HEADER_LENGTH bytes."""
        header = CommandHeader(
            destination=DESTINATION_CAMERA,
            command_id=0x00,
            category=0x0A,
            parameter=0x01,
            data_type=DataType.VOID,
            operation=Operation.ASSIGN,
        )
        packet = encode_packet(header)

        assert packet == bytes([0xFF, 0x04, 0x00, 0x00, 0x0A, 0x01, 0x00, 0x00])
        assert len(packet) == HEADER_LENGTH

    def test_encode_packet_with_payload_sets_length_byte(self):
        """The length byte counts only category/parameter/data_type/operation plus payload."""
        header = CommandHeader(
            destination=DESTINATION_CAMERA,
            command_id=0x00,
            category=0x0A,
            parameter=0x01,
            data_type=DataType.BOOL,
            operation=Operation.ASSIGN,
        )
        packet = encode_packet(header, payload=b"\x01")

        assert packet[1] == 0x05  # 4 fixed bytes (category..operation) + 1 payload byte
        assert packet[8:] == b"\x01"

    def test_encode_packet_uses_reserved_byte_default(self):
        """The reserved byte defaults to RESERVED_BYTE when not overridden."""
        header = CommandHeader(
            destination=DESTINATION_CAMERA,
            command_id=0x00,
            category=0x0A,
            parameter=0x01,
            data_type=DataType.VOID,
            operation=Operation.ASSIGN,
        )
        packet = encode_packet(header)

        assert packet[3] == RESERVED_BYTE

    def test_encode_packet_raises_when_payload_too_large(self):
        """A payload that would push the length byte past 255 raises ValueError."""
        header = CommandHeader(
            destination=DESTINATION_CAMERA,
            command_id=0x00,
            category=0x0A,
            parameter=0x01,
            data_type=DataType.STRING,
            operation=Operation.ASSIGN,
        )
        with pytest.raises(ValueError, match="too large"):
            encode_packet(header, payload=bytes(255))

    def test_encode_packet_matches_real_pocket_6k_g2_record_start_capture(self):
        """Reproduces the exact bytes reverse-engineered for POCKET_6K_G2 v7.9 record start.

        Cross-check against a real captured command: FF 05 00 01 0A 01 01 00 02.
        """
        header = CommandHeader(
            destination=DESTINATION_CAMERA,
            command_id=0x00,
            category=0x0A,
            parameter=0x01,
            data_type=DataType.BOOL,
            operation=Operation.ASSIGN,
            reserved=0x01,
        )
        packet = encode_packet(header, payload=bytes([0x02]))

        assert packet == bytes([0xFF, 0x05, 0x00, 0x01, 0x0A, 0x01, 0x01, 0x00, 0x02])

    def test_encode_packet_matches_real_pocket_6k_g2_record_stop_capture(self):
        """Reproduces the exact bytes reverse-engineered for POCKET_6K_G2 v7.9 record stop.

        Cross-check against a real captured command: FF 05 00 01 0A 01 01 00 00.
        """
        header = CommandHeader(
            destination=DESTINATION_CAMERA,
            command_id=0x00,
            category=0x0A,
            parameter=0x01,
            data_type=DataType.BOOL,
            operation=Operation.ASSIGN,
            reserved=0x01,
        )
        packet = encode_packet(header, payload=bytes([0x00]))

        assert packet == bytes([0xFF, 0x05, 0x00, 0x01, 0x0A, 0x01, 0x01, 0x00, 0x00])


class TestDecodePacket:
    """Tests for ``decode_packet``."""

    def test_decode_packet_round_trips_with_encode(self):
        """Decoding an encoded packet reproduces the original header and payload."""
        header = CommandHeader(
            destination=DESTINATION_CAMERA,
            command_id=0x00,
            category=0x0A,
            parameter=0x01,
            data_type=DataType.INT16,
            operation=Operation.OFFSET,
        )
        payload = b"\x10\x00"
        packet = encode_packet(header, payload=payload)

        decoded_header, decoded_payload = decode_packet(packet)

        assert decoded_header == header
        assert decoded_payload == payload

    def test_decode_packet_accepts_camera_report_operation(self):
        """Operation 0x02 (CAMERA_REPORT) is sniffer-verified on real notifications."""
        header = CommandHeader(
            destination=DESTINATION_CAMERA,
            command_id=0x00,
            category=0x0A,
            parameter=0x01,
            data_type=DataType.BOOL,
            operation=Operation.CAMERA_REPORT,
        )
        packet = encode_packet(header, payload=b"\x02")

        decoded_header, decoded_payload = decode_packet(packet)

        assert decoded_header.operation == Operation.CAMERA_REPORT
        assert decoded_payload == b"\x02"

    def test_decode_packet_raises_when_too_short(self):
        """A buffer shorter than HEADER_LENGTH raises ValueError."""
        with pytest.raises(ValueError, match="too short"):
            decode_packet(bytes([0x00, 0x06, 0x00]))

    def test_decode_packet_raises_on_length_byte_mismatch(self):
        """A declared length that doesn't match the actual buffer size raises ValueError."""
        packet = bytes([0xFF, 0x09, 0x00, 0x00, 0x0A, 0x01, 0x00, 0x00])
        with pytest.raises(ValueError, match="Length byte mismatch"):
            decode_packet(packet)

    def test_decode_packet_raises_on_unknown_data_type(self):
        """An out-of-range data type byte raises ValueError."""
        packet = bytes([0xFF, 0x04, 0x00, 0x00, 0x0A, 0x01, 0xFF, 0x00])
        with pytest.raises(ValueError, match="Unknown data type"):
            decode_packet(packet)

    def test_decode_packet_raises_on_unknown_operation(self):
        """An out-of-range operation byte raises ValueError."""
        packet = bytes([0xFF, 0x04, 0x00, 0x00, 0x0A, 0x01, 0x00, 0xFF])
        with pytest.raises(ValueError, match="Unknown operation"):
            decode_packet(packet)

    def test_decode_packet_preserves_nonzero_reserved_byte(self):
        """A non-zero reserved byte from real hardware is surfaced, not discarded."""
        packet = bytes([0xFF, 0x04, 0x00, 0x2A, 0x0A, 0x01, 0x00, 0x00])
        header, _ = decode_packet(packet)

        assert header.reserved == 0x2A

    def test_decode_packet_matches_real_pocket_6k_g2_record_start_capture(self):
        """Decodes the exact real POCKET_6K_G2 v7.9 record start command bytes."""
        packet = bytes([0xFF, 0x05, 0x00, 0x01, 0x0A, 0x01, 0x01, 0x00, 0x02])

        header, payload = decode_packet(packet)

        assert header.category == 0x0A
        assert header.parameter == 0x01
        assert header.data_type == DataType.BOOL
        assert header.operation == Operation.ASSIGN
        assert header.reserved == 0x01
        assert payload == bytes([0x02])

    def test_decode_packet_matches_real_pocket_6k_g2_record_stop_capture(self):
        """Decodes the exact real POCKET_6K_G2 v7.9 record stop command bytes."""
        packet = bytes([0xFF, 0x05, 0x00, 0x01, 0x0A, 0x01, 0x01, 0x00, 0x00])

        header, payload = decode_packet(packet)

        assert header.category == 0x0A
        assert header.parameter == 0x01
        assert header.data_type == DataType.BOOL
        assert header.operation == Operation.ASSIGN
        assert header.reserved == 0x01
        assert payload == bytes([0x00])


class TestEncodeAssign:
    """Tests for the category-agnostic ``encode_assign``."""

    def test_reproduces_known_g2_record_start_packet(self):
        """Byte-for-byte match with the sniffer-verified POCKET_6K_G2 v7.9
        record-start command (see docs/protocol.md §6)."""
        packet = encode_assign(
            category=0x0A, parameter=0x01, data_type=DataType.BOOL, value=2, reserved=0x01
        )

        assert packet == bytes([0xFF, 0x05, 0x00, 0x01, 0x0A, 0x01, 0x01, 0x00, 0x02])

    def test_reserved_defaults_to_codec_reserved_byte(self):
        packet = encode_assign(category=0x0A, parameter=0x01, data_type=DataType.BOOL, value=0)

        assert packet[3] == RESERVED_BYTE

    def test_multibyte_value_is_little_endian(self):
        packet = encode_assign(
            category=0x01, parameter=0x0E, data_type=DataType.INT32, value=0x00000320
        )

        assert packet[8:] == bytes([0x20, 0x03, 0x00, 0x00])

    def test_unsupported_data_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported data type"):
            encode_assign(category=0x0A, parameter=0x01, data_type=DataType.STRING, value=1)
