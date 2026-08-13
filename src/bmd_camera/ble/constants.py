"""
bmd_camera/ble/constants.py
================
BLE transport-level constants for the Blackmagic BLE Camera Control API.

WHAT BELONGS HERE
─────────────────
Only values that are fixed by the Bluetooth / BMD BLE spec, apply to every
camera model and firmware version, and concern the BLE transport itself
(not the BMD command packet protocol carried over it):
  • BLE service / characteristic UUIDs (baseline — overridden per-model in JSON)
  • Characteristic human-readable name lookup (for logging/debugging)
  • BLE timing constants (scan, connect, reconnect)

WHAT DOES NOT BELONG HERE
──────────────────────────
  • BMD command packet header structure (destination byte, reserved byte,
    operation codes) — lives in protocol/codec.py
  • BMD payload data type constants — lives in protocol/types.py
  • Category and parameter byte values for a specific command family —
    live in protocol/categories/<category>.py, added only after a sniffer
    capture confirms them on real hardware
  • Model-specific values (codec IDs, variant IDs, FPS integers, storage
    characteristic UUIDs) — live exclusively in
    payloads/models/<MODEL>_<FW>.json and are accessed via CameraProfile
"""

# ─────────────────────────────────────────────────────────────────────────────
# BLE GATT UUIDs (baseline — model JSONs override CHARACTERISTIC_INCOMING)
# ─────────────────────────────────────────────────────────────────────────────
BLUETOOTH_BASE_UUID = "0000{short}-0000-1000-8000-00805f9b34fb"


def normalize_uuid(uuid: str) -> str:
    uuid = uuid.lower()
    if len(uuid) == 4:
        return BLUETOOTH_BASE_UUID.format(short=uuid)
    return uuid


GENERIC_ACCESS_PROFILE_SERVICE_UUID = normalize_uuid("1800")
# Standard Bluetooth Generic Access Profile (GAP) service.
# Provides basic device identity information such as device name and appearance.
GAP_CHARACTERISTIC_DEVICE_NAME = normalize_uuid("2a00")  # Read
# Readable characteristic that exposes the BLE device's advertised/display name.
GAP_CHARACTERISTIC_APPEARANCE = normalize_uuid("2a01")  # Read
# Readable characteristic that identifies the generic device category/type.

DEVICE_INFO_SERVICE_UUID = normalize_uuid("180a")
# Standard Bluetooth Device Information Service.
# Provides manufacturer and model information for the camera.
CHARACTERISTIC_MANUFACTURER_INFO = normalize_uuid("2a29")  # Read
# Readable characteristic that returns the manufacturer name, expected to be
# "Blackmagic Design" for Blackmagic cameras
CHARACTERISTIC_MODEL_INFO = normalize_uuid("2a24")  # Read
# Readable characteristic that returns the camera model name.

BMD_SERVICE_UUID = "291d567a-6d75-11e6-8b77-86f30ca893d3"
CHARACTERISTIC_OUTGOING = "5dd3465f-1aee-4299-8493-d2eca2f8e1bb"  # Write
# Send Camera Control messages.
CHARACTERISTIC_INCOMING = "b864e140-76a0-416a-bf30-5876504537d9"  # Indicate
# Request notifications for this characteristic to receive Camera Control messages from
# the camera.
CHARACTERISTIC_TIMECODE = "6d8f2110-86f1-41bf-9afb-451d87e976c8"  # Notify
# Request notifications for this characteristic to receive timecode updates.
# Timecode (HH:MM:SS:mm) is represented by a 32-bit BCD number: (eg. 09:12:53:10 =
# 0x09125310)
CHARACTERISTIC_CAM_STATUS = "7fe8691d-95dc-4fc5-8abd-ca74339b51b9"
# Read, Notify, Write
# Request notifications for this characteristic to receive camera status updates.
# The camera status is represented by flags contained in an 8-bit integer:
# None = 0x00
# Camera Power On = 0x01
# Connected = 0x02
# Paired = 0x04
# Versions Verified = 0x08
# Initial Payload Received = 0x10
# Camera Ready = = 0x20
# Send a value of 0x00 to power a connected camera off.
# Send a value of 0x01 to power a connected camera on.
CHARACTERISTIC_PROTO_VER = "8f1fd018-b508-456f-8f82-3d392bee2706"  # Read
# Read this value to determine the camera’s supported CCU protocol version
CHARACTERISTIC_BMD_DEVICE_NAME = "ffac0c52-c9fb-41a0-b063-cc76282eb89c"  # Write
# Send a device name to the camera (max. 32 characters).
# The camera will display this name in the Bluetooth Setup Menu.

CHARACTERISTIC_NAMES: dict = {
    GAP_CHARACTERISTIC_DEVICE_NAME: "DEVICE_NAME (Read)",
    GAP_CHARACTERISTIC_APPEARANCE: "APPEARANCE (Read)",
    CHARACTERISTIC_MANUFACTURER_INFO: "CAMERA MANUFACTURER INFO(Read)",
    CHARACTERISTIC_MODEL_INFO: "CAMERA MODEL INFO(Read)",
    CHARACTERISTIC_OUTGOING: "OUTGOING_CONTROL (Write)",
    CHARACTERISTIC_INCOMING: "INCOMING_CONTROL (Indicate)",
    CHARACTERISTIC_TIMECODE: "TIMECODE (Notify)",
    CHARACTERISTIC_CAM_STATUS: "CAMERA_STATUS (Notify)",
    CHARACTERISTIC_PROTO_VER: "PROTOCOL_VERSION (Read)",
    CHARACTERISTIC_BMD_DEVICE_NAME: "BMD_DEVICE_NAME (Write)",
}


# ─────────────────────────────────────────────────────────────────────────────
# Timing constants
# ─────────────────────────────────────────────────────────────────────────────

BLE_SCAN_TIMEOUT_S = 15
BLE_CONNECT_TIMEOUT_S = 10.0
RECONNECT_MAX_ATTEMPTS = 3
RECONNECT_DELAY_S = 5.0

# connect() retries the whole connect + subscribe_all sequence when the camera
# drops the link during the initial CCCD writes. Observed on POCKET_6K_G2 v8.6
# (2026-07-29): 3 of 6 connects dropped ~300 ms after the link came up, and the
# outcome tracked how long the connect itself took (every connect under 2 s
# survived; every one over 2.2 s dropped) rather than anything about the command
# being sent — i.e. transient and worth retrying, not a deterministic refusal.
CONNECT_SUBSCRIBE_MAX_ATTEMPTS = 3
CONNECT_RETRY_DELAY_S = 2.0
