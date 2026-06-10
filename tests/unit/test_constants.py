"""Unit tests for :mod:`bmd_ble.constants`.

This module verifies protocol-level constants exposed by ``bmd_ble.constants``.
The constants module contains values that are fixed by the Blackmagic Design BLE
Camera Control protocol, including Bluetooth UUIDs, characteristic labels, and
BLE timing defaults.

The tests are class-based and grouped by responsibility so failures are easier
to triage:

* ``TestUuidNormalization`` checks short UUID expansion and lowercase handling.
* ``TestServiceUuids`` checks BLE service UUID constants.
* ``TestCharacteristicUuids`` checks BLE characteristic UUID constants.
* ``TestCharacteristicNames`` checks the characteristic display-name mapping.
* ``TestBleTimingConstants`` checks scan/connect timeout defaults.

Exact protocol values are asserted intentionally. If one of these tests fails,
review whether the protocol constant really changed before updating the test.
"""

import re

from bmd_ble import constants


# Matches normalized lowercase 128-bit UUID strings, for example:
# "291d567a-6d75-11e6-8b77-86f30ca893d3".
UUID_128_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class TestUuidNormalization:
    """Tests for the ``constants.normalize_uuid`` helper function."""

    def test_normalize_uuid_expands_16_bit_uuid_to_bluetooth_base_uuid(self):
        """A lowercase 16-bit UUID is expanded to the Bluetooth base UUID."""
        assert (
            constants.normalize_uuid("180a")
            == "0000180a-0000-1000-8000-00805f9b34fb"
        )

    def test_normalize_uuid_is_case_insensitive_for_16_bit_uuid(self):
        """An uppercase 16-bit UUID is lowercased before expansion."""
        assert (
            constants.normalize_uuid("2A29")
            == "00002a29-0000-1000-8000-00805f9b34fb"
        )

    def test_normalize_uuid_lowercases_and_preserves_128_bit_uuid(self):
        """A 128-bit UUID keeps its structure and is normalized to lowercase."""
        assert (
            constants.normalize_uuid("291D567A-6D75-11E6-8B77-86F30CA893D3")
            == "291d567a-6d75-11e6-8b77-86f30ca893d3"
        )


class TestServiceUuids:
    """Tests for BLE service UUID constants."""

    def test_bluetooth_base_uuid_template(self):
        """The Bluetooth base UUID template contains the ``short`` placeholder."""
        assert constants.BLUETOOTH_BASE_UUID == "0000{short}-0000-1000-8000-00805f9b34fb"

    def test_standard_service_uuids_are_normalized_128_bit_uuids(self):
        """Standard Bluetooth service UUIDs are exposed as full 128-bit UUIDs."""
        assert (
            constants.GENERIC_ACCESS_PROFILE_SERVICE_UUID
            == "00001800-0000-1000-8000-00805f9b34fb"
        )
        assert constants.BMD_INFO_SERVICE_UUID == "0000180a-0000-1000-8000-00805f9b34fb"

        assert UUID_128_RE.match(constants.GENERIC_ACCESS_PROFILE_SERVICE_UUID)
        assert UUID_128_RE.match(constants.BMD_INFO_SERVICE_UUID)

    def test_bmd_service_uuid_is_valid_128_bit_uuid(self):
        """The BMD camera-control service is a fixed vendor-specific UUID."""
        assert constants.BMD_SERVICE_UUID == "291d567a-6d75-11e6-8b77-86f30ca893d3"
        assert UUID_128_RE.match(constants.BMD_SERVICE_UUID)


class TestCharacteristicUuids:
    """Tests for BLE characteristic UUID constants."""

    def test_gap_characteristic_uuids_are_normalized_128_bit_uuids(self):
        """Generic Access Profile characteristic UUIDs are normalized."""
        assert constants.GAP_CHARACTERISTIC_DEVICE_NAME == "00002a00-0000-1000-8000-00805f9b34fb"
        assert constants.GAP_CHARACTERISTIC_APPEARANCE == "00002a01-0000-1000-8000-00805f9b34fb"

        assert UUID_128_RE.match(constants.GAP_CHARACTERISTIC_DEVICE_NAME)
        assert UUID_128_RE.match(constants.GAP_CHARACTERISTIC_APPEARANCE)

    def test_standard_info_characteristic_uuids_are_normalized_128_bit_uuids(self):
        """Device Information Service characteristic UUIDs are normalized."""
        assert constants.CHARACTERISTIC_MANUFACTURER_INFO == "00002a29-0000-1000-8000-00805f9b34fb"
        assert constants.CHARACTERISTIC_MODEL_INFO == "00002a24-0000-1000-8000-00805f9b34fb"

        assert UUID_128_RE.match(constants.CHARACTERISTIC_MANUFACTURER_INFO)
        assert UUID_128_RE.match(constants.CHARACTERISTIC_MODEL_INFO)

    def test_bmd_control_characteristic_uuids_are_valid_128_bit_uuids(self):
        """All BMD camera-control characteristics are valid 128-bit UUIDs."""
        characteristic_uuids = [
            constants.CHARACTERISTIC_OUTGOING,
            constants.CHARACTERISTIC_INCOMING,
            constants.CHARACTERISTIC_TIMECODE,
            constants.CHARACTERISTIC_CAM_STATUS,
            constants.CHARACTERISTIC_PROTO_VER,
            constants.CHARACTERISTIC_BMD_DEVICE_NAME,
        ]

        for uuid in characteristic_uuids:
            assert isinstance(uuid, str)
            assert UUID_128_RE.match(uuid)

    def test_bmd_control_characteristic_uuids_have_expected_values(self):
        """BMD camera-control characteristic UUIDs match protocol values."""
        assert constants.CHARACTERISTIC_OUTGOING == "5dd3465f-1aee-4299-8493-d2eca2f8e1bb"
        assert constants.CHARACTERISTIC_INCOMING == "b864e140-76a0-416a-bf30-5876504537d9"
        assert constants.CHARACTERISTIC_TIMECODE == "6d8f2110-86f1-41bf-9afb-451d87e976c8"
        assert constants.CHARACTERISTIC_CAM_STATUS == "7fe8691d-95dc-4fc5-8abd-ca74339b51b9"
        assert constants.CHARACTERISTIC_PROTO_VER == "8f1fd018-b508-456f-8f82-3d392bee2706"
        assert constants.CHARACTERISTIC_BMD_DEVICE_NAME == "ffac0c52-c9fb-41a0-b063-cc76282eb89c"


class TestCharacteristicNames:
    """Tests for the ``CHARACTERISTIC_NAMES`` lookup table."""

    def test_characteristic_names_contains_all_defined_characteristics(self):
        """Every characteristic constant has a human-readable label."""
        expected_characteristics = {
            constants.GAP_CHARACTERISTIC_DEVICE_NAME,
            constants.GAP_CHARACTERISTIC_APPEARANCE,
            constants.CHARACTERISTIC_MANUFACTURER_INFO,
            constants.CHARACTERISTIC_MODEL_INFO,
            constants.CHARACTERISTIC_OUTGOING,
            constants.CHARACTERISTIC_INCOMING,
            constants.CHARACTERISTIC_TIMECODE,
            constants.CHARACTERISTIC_CAM_STATUS,
            constants.CHARACTERISTIC_PROTO_VER,
            constants.CHARACTERISTIC_BMD_DEVICE_NAME,
        }

        assert expected_characteristics.issubset(constants.CHARACTERISTIC_NAMES.keys())

    def test_characteristic_names_are_human_readable_strings(self):
        """Lookup keys are UUIDs and lookup values are non-empty labels."""
        for uuid, name in constants.CHARACTERISTIC_NAMES.items():
            assert isinstance(uuid, str)
            assert UUID_128_RE.match(uuid)
            assert isinstance(name, str)
            assert name.strip()
            assert "(" in name and ")" in name

    def test_expected_characteristic_labels_are_present(self):
        """Each known characteristic maps to its expected display label."""
        assert constants.CHARACTERISTIC_NAMES[constants.GAP_CHARACTERISTIC_DEVICE_NAME] == "DEVICE_NAME (Read)"
        assert constants.CHARACTERISTIC_NAMES[constants.GAP_CHARACTERISTIC_APPEARANCE] == "APPEARANCE (Read)"
        assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_MANUFACTURER_INFO] == (
            "CAMERA MANUFACTURER INFO(Read)"
        )
        assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_MODEL_INFO] == (
            "CAMERA MODEL INFO(Read)"
        )
        assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_OUTGOING] == (
            "OUTGOING_CONTROL (Write)"
        )
        assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_INCOMING] == (
            "INCOMING_CONTROL (Indicate)"
        )
        assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_TIMECODE] == "TIMECODE (Notify)"
        assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_CAM_STATUS] == (
            "CAMERA_STATUS (Notify)"
        )
        assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_PROTO_VER] == (
            "PROTOCOL_VERSION (Read)"
        )
        assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_BMD_DEVICE_NAME] == (
            "BMD_DEVICE_NAME (Write)"
        )

    def test_characteristic_names_has_no_duplicate_uuid_keys(self):
        """The lookup table does not contain duplicate UUID keys."""
        keys = list(constants.CHARACTERISTIC_NAMES.keys())

        assert len(keys) == len(set(keys))


class TestBleTimingConstants:
    """Tests for BLE timing defaults."""

    def test_ble_timing_constants_are_positive_numbers(self):
        """BLE timeout constants are positive numeric values."""
        assert isinstance(constants.BLE_SCAN_TIMEOUT_S, int)
        assert isinstance(constants.BLE_CONNECT_TIMEOUT_S, float)

        assert constants.BLE_SCAN_TIMEOUT_S > 0
        assert constants.BLE_CONNECT_TIMEOUT_S > 0

    def test_ble_timing_constants_have_expected_defaults(self):
        """BLE timeout constants retain their expected default values."""
        assert constants.BLE_SCAN_TIMEOUT_S == 15
        assert constants.BLE_CONNECT_TIMEOUT_S == 10.0
