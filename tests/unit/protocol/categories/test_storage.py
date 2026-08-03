"""Unit tests for :mod:`bmd_camera.ble.protocol.categories.storage`.

These tests exercise generic decode mechanics, not model-specific truth.
``CATEGORY``/``PARAMETER``/``BYTE_OFFSET`` happen to coincide with the real,
sniffer-observed CANDIDATE write-margin-warning signal (see
payloads/models/POCKET_6K_G2_v7.9.json's ``storage.write_margin_warning``
and docs/ble/recording.md) but that's incidental to what's tested here.
"""

import pytest

from bmd_camera.ble.protocol.categories.storage import decode_write_margin, is_storage_notification
from bmd_camera.ble.protocol.codec import CommandHeader, Operation, decode_packet, encode_packet
from bmd_camera.ble.protocol.types import DataType

CATEGORY = 0x09
PARAMETER = 0x01
BYTE_OFFSET = 1


def _packet(payload: bytes) -> bytes:
    header = CommandHeader(
        destination=0xFF,
        command_id=0x00,
        reserved=0x00,
        category=CATEGORY,
        parameter=PARAMETER,
        data_type=DataType.INT8,
        operation=Operation.CAMERA_REPORT,
    )
    return encode_packet(header, payload=payload)


class TestIsStorageNotification:
    def test_matches_expected_category_and_parameter(self):
        header, _ = decode_packet(_packet(bytes([0x00, 0x01, 0x00])))

        assert is_storage_notification(header, category=CATEGORY, parameter=PARAMETER)

    def test_does_not_match_other_category(self):
        header, _ = decode_packet(_packet(bytes([0x00, 0x01, 0x00])))

        assert not is_storage_notification(header, category=0x0A, parameter=PARAMETER)

    def test_does_not_match_other_parameter(self):
        header, _ = decode_packet(_packet(bytes([0x00, 0x01, 0x00])))

        assert not is_storage_notification(header, category=CATEGORY, parameter=0x02)


class TestDecodeWriteMargin:
    def test_decodes_nominal_value_at_offset_1(self):
        """Real capture: 00 01 00 -> nominal (1)."""
        payload = bytes([0x00, 0x01, 0x00])

        assert decode_write_margin(payload, DataType.INT8, byte_offset=BYTE_OFFSET) == 1

    def test_decodes_low_margin_value_at_offset_1(self):
        """Real capture: 00 FE 00 -> low_margin (-2, signed)."""
        payload = bytes([0x00, 0xFE, 0x00])

        assert decode_write_margin(payload, DataType.INT8, byte_offset=BYTE_OFFSET) == -2

    def test_decodes_at_offset_0_when_requested(self):
        payload = bytes([0x05, 0x00, 0x00])

        assert decode_write_margin(payload, DataType.INT8, byte_offset=0) == 5

    def test_raises_on_payload_shorter_than_offset_plus_width(self):
        with pytest.raises(ValueError, match="Expected at least 2-byte payload"):
            decode_write_margin(bytes([0x00]), DataType.INT8, byte_offset=1)

    def test_raises_for_unsupported_data_type(self):
        with pytest.raises(ValueError, match="Unsupported data type"):
            decode_write_margin(bytes([0x00, 0x01, 0x00]), DataType.STRING, byte_offset=1)
