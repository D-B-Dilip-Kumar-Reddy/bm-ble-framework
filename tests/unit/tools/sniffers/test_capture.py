"""Unit tests for tools/sniffers/capture.py — pure decode/dedup logic only.

No BLE, no input(), no hardware, no filesystem — matches tests/unit/'s
"no hardware, full mocking" rule. tools/ is not a package (no __init__.py,
scripts run standalone), so tools/sniffers is added to sys.path directly,
mirroring the sys.path trick tools/query/ble_services_chars.py already uses.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "sniffers"))

from capture import DecodedNotification, decode_notification, dedupe_triples  # noqa: E402

from bmd_ble.constants import CHARACTERISTIC_CAM_STATUS, CHARACTERISTIC_INCOMING  # noqa: E402


def _characteristic(uuid: str):
    return SimpleNamespace(uuid=uuid)


class TestDecodeNotification:
    def test_well_formed_incoming_control_packet_decodes_category_and_parameter(self):
        data = bytearray([0x00, 0x07, 0x00, 0x00, 0x0A, 0x01, 0x01, 0x00, 0x01])

        result = decode_notification(_characteristic(CHARACTERISTIC_INCOMING), data)

        assert result.category == 0x0A
        assert result.parameter == 0x01
        assert result.data_type == "BOOL"
        assert result.operation == "ASSIGN"
        assert result.payload_hex == "01"
        assert result.decode_error is None
        assert result.characteristic_name == "INCOMING_CONTROL (Indicate)"
        assert result.raw_hex == "00 07 00 00 0A 01 01 00 01"

    def test_camera_status_one_byte_payload_decodes_with_error_set_not_raised(self):
        data = bytearray([0x3F])

        result = decode_notification(_characteristic(CHARACTERISTIC_CAM_STATUS), data)

        assert result.category is None
        assert result.parameter is None
        assert result.decode_error is not None
        assert result.characteristic_name == "CAMERA_STATUS (Notify)"
        assert result.raw_hex == "3F"

    def test_unknown_characteristic_uuid_falls_back_to_placeholder_name(self):
        result = decode_notification(_characteristic("dead-beef"), bytearray([0x00]))

        assert result.characteristic_name == "UNKNOWN (dead-beef)"


class TestDedupeTriples:
    def test_repeated_identical_triples_collapse_preserving_order(self):
        incoming = DecodedNotification(
            timestamp="t1",
            characteristic_uuid=CHARACTERISTIC_INCOMING,
            characteristic_name="INCOMING_CONTROL (Indicate)",
            raw_hex="",
            category=0x0A,
            parameter=0x01,
            data_type="BOOL",
            operation="ASSIGN",
            payload_hex="01",
            decode_error=None,
        )
        status = DecodedNotification(
            timestamp="t2",
            characteristic_uuid=CHARACTERISTIC_CAM_STATUS,
            characteristic_name="CAMERA_STATUS (Notify)",
            raw_hex="",
            category=None,
            parameter=None,
            data_type=None,
            operation=None,
            payload_hex=None,
            decode_error="not a command packet",
        )
        duplicate_incoming = DecodedNotification(
            timestamp="t3",
            characteristic_uuid=CHARACTERISTIC_INCOMING,
            characteristic_name="INCOMING_CONTROL (Indicate)",
            raw_hex="",
            category=0x0A,
            parameter=0x01,
            data_type="BOOL",
            operation="ASSIGN",
            payload_hex="01",
            decode_error=None,
        )

        result = dedupe_triples([incoming, status, duplicate_incoming])

        assert result == [
            ("INCOMING_CONTROL (Indicate)", 0x0A, 0x01),
            ("CAMERA_STATUS (Notify)", None, None),
        ]

    def test_empty_notifications_returns_empty_list(self):
        assert dedupe_triples([]) == []
