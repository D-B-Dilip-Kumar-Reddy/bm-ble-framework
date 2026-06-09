import re

from bmd_ble import constants


UUID_128_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

UUID_16_RE = re.compile(r"^[0-9a-f]{4}$")


def test_bmd_info_service_uuid_is_valid_16_bit_uuid():
    assert constants.BMD_INFO_SERVICE_UUID == "180a"
    assert UUID_16_RE.match(constants.BMD_INFO_SERVICE_UUID)


def test_standard_info_characteristic_uuids_are_valid_16_bit_uuids():
    assert constants.CHARACTERISTIC_MANUFACTURER_INFO == "2a29"
    assert constants.CHARACTERISTIC_MODEL_INFO == "2a24"

    assert UUID_16_RE.match(constants.CHARACTERISTIC_MANUFACTURER_INFO)
    assert UUID_16_RE.match(constants.CHARACTERISTIC_MODEL_INFO)


def test_bmd_service_uuid_is_valid_128_bit_uuid():
    assert constants.BMD_SERVICE_UUID == "291d567a-6d75-11e6-8b77-86f30ca893d3"
    assert UUID_128_RE.match(constants.BMD_SERVICE_UUID)


def test_control_characteristic_uuids_are_valid_128_bit_uuids():
    characteristic_uuids = [
        constants.CHARACTERISTIC_OUTGOING,
        constants.CHARACTERISTIC_INCOMING,
        constants.CHARACTERISTIC_TIMECODE,
        constants.CHARACTERISTIC_CAM_STATUS,
        constants.CHARACTERISTIC_DEVICE_NAME,
        constants.CHARACTERISTIC_PROTO_VER,
    ]

    for uuid in characteristic_uuids:
        assert isinstance(uuid, str)
        assert UUID_128_RE.match(uuid)


def test_characteristic_names_contains_all_defined_characteristics():
    expected_characteristics = {
        constants.CHARACTERISTIC_MANUFACTURER_INFO,
        constants.CHARACTERISTIC_MODEL_INFO,
        constants.CHARACTERISTIC_OUTGOING,
        constants.CHARACTERISTIC_INCOMING,
        constants.CHARACTERISTIC_TIMECODE,
        constants.CHARACTERISTIC_CAM_STATUS,
        constants.CHARACTERISTIC_DEVICE_NAME,
        constants.CHARACTERISTIC_PROTO_VER,
    }

    assert expected_characteristics.issubset(constants.CHARACTERISTIC_NAMES.keys())


def test_characteristic_names_are_human_readable_strings():
    for uuid, name in constants.CHARACTERISTIC_NAMES.items():
        assert isinstance(uuid, str)
        assert isinstance(name, str)
        assert name.strip()
        assert "(" in name and ")" in name


def test_expected_characteristic_labels_are_present():
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
    assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_TIMECODE] == (
        "TIMECODE (Notify)"
    )
    assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_CAM_STATUS] == (
        "CAMERA_STATUS (Notify)"
    )
    assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_DEVICE_NAME] == (
        "DEVICE_NAME (Read)"
    )
    assert constants.CHARACTERISTIC_NAMES[constants.CHARACTERISTIC_PROTO_VER] == (
        "PROTOCOL_VERSION (Read)"
    )


def test_characteristic_names_has_no_duplicate_uuid_keys():
    keys = list(constants.CHARACTERISTIC_NAMES.keys())

    assert len(keys) == len(set(keys))


def test_ble_timing_constants_are_positive_numbers():
    assert isinstance(constants.BLE_SCAN_TIMEOUT_S, int)
    assert isinstance(constants.BLE_CONNECT_TIMEOUT_S, float)

    assert constants.BLE_SCAN_TIMEOUT_S > 0
    assert constants.BLE_CONNECT_TIMEOUT_S > 0


def test_ble_timing_constants_have_expected_defaults():
    assert constants.BLE_SCAN_TIMEOUT_S == 15
    assert constants.BLE_CONNECT_TIMEOUT_S == 10.0