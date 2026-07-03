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

        assert packet == bytes([0x00, 0x06, 0x00, 0x00, 0x0A, 0x01, 0x00, 0x00])
        assert len(packet) == HEADER_LENGTH

    def test_encode_packet_with_payload_sets_length_byte(self):
        """The length byte accounts for the 6 fixed header bytes plus the payload."""
        header = CommandHeader(
            destination=DESTINATION_CAMERA,
            command_id=0x00,
            category=0x0A,
            parameter=0x01,
            data_type=DataType.BOOL,
            operation=Operation.ASSIGN,
        )
        packet = encode_packet(header, payload=b"\x01")

        assert packet[1] == 0x07  # 6 header-remainder bytes + 1 payload byte
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

    def test_decode_packet_raises_when_too_short(self):
        """A buffer shorter than HEADER_LENGTH raises ValueError."""
        with pytest.raises(ValueError, match="too short"):
            decode_packet(bytes([0x00, 0x06, 0x00]))

    def test_decode_packet_raises_on_length_byte_mismatch(self):
        """A declared length that doesn't match the actual buffer size raises ValueError."""
        packet = bytes([0x00, 0x09, 0x00, 0x00, 0x0A, 0x01, 0x00, 0x00])
        with pytest.raises(ValueError, match="Length byte mismatch"):
            decode_packet(packet)

    def test_decode_packet_raises_on_unknown_data_type(self):
        """An out-of-range data type byte raises ValueError."""
        packet = bytes([0x00, 0x06, 0x00, 0x00, 0x0A, 0x01, 0xFF, 0x00])
        with pytest.raises(ValueError, match="Unknown data type"):
            decode_packet(packet)

    def test_decode_packet_raises_on_unknown_operation(self):
        """An out-of-range operation byte raises ValueError."""
        packet = bytes([0x00, 0x06, 0x00, 0x00, 0x0A, 0x01, 0x00, 0xFF])
        with pytest.raises(ValueError, match="Unknown operation"):
            decode_packet(packet)

    def test_decode_packet_preserves_nonzero_reserved_byte(self):
        """A non-zero reserved byte from real hardware is surfaced, not discarded."""
        packet = bytes([0x00, 0x06, 0x00, 0x2A, 0x0A, 0x01, 0x00, 0x00])
        header, _ = decode_packet(packet)

        assert header.reserved == 0x2A
