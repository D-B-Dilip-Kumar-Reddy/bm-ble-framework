"""
bmd_ble/constants.py
================
Protocol-level constants for the Blackmagic BLE Camera Control API.

WHAT BELONGS HERE
─────────────────
Only values that are fixed by the BMD BLE protocol spec and do NOT vary
between camera models or firmware versions:
  • BLE service / characteristic UUIDs (baseline — overridden per-model in JSON)
  • Packet structure constants (direction bytes, alignment)
  • Data type and operation identifiers
  • Category and parameter byte values
  • Transport / storage mode values
  • Status bitfield definitions
  • Frame-rate encoding constants (verified from live sniffer data)
  • Timing constants

WHAT DOES NOT BELONG HERE
──────────────────────────
Model-specific values (codec IDs, variant IDs, FPS integers) live exclusively
in payloads/models/<MODEL>_<FW>.json and are accessed via CameraProfile.
"""

# ─────────────────────────────────────────────────────────────────────────────
# BLE GATT UUIDs (baseline — model JSONs override CHARACTERISTIC_INCOMING)
# ─────────────────────────────────────────────────────────────────────────────

BMD_INFO_SERVICE_UUID = "180a"
CHARACTERISTIC_MANUFACTURER_INFO = "2a29"
CHARACTERISTIC_MODEL_INFO = "2a24"

BMD_SERVICE_UUID          =     "291d567a-6d75-11e6-8b77-86f30ca893d3"
CHARACTERISTIC_OUTGOING   =     "5dd3465f-1aee-4299-8493-d2eca2f8e1bb"  # Write
CHARACTERISTIC_INCOMING   =     "b864e140-76a0-416a-bf30-5876504537d9"  # Indicate
CHARACTERISTIC_TIMECODE   =     "6d8f2110-86f1-41bf-9afb-451d87e976c8"  # Notify
CHARACTERISTIC_CAM_STATUS =     "7fe8691d-95dc-4fc5-8abd-ca74339b51b9"  # Notify
CHARACTERISTIC_DEVICE_NAME =    "ffac0c52-c9fb-41a0-b063-cc76282eb89c"
CHARACTERISTIC_PROTO_VER  =     "8f1fd018-b508-456f-8f82-3d392bee2706"  # Read
# CHARACTERISTIC_DEVICE_NAME=     "00002a00-0000-1000-8000-00805f9b34fb"  # Read
# CHARACTERISTIC_UNKNOWN_WRITE =  "ffac0c52-c9fb-41a0-b063-cc76282eb89c"  # Write,
# purpose TBD

CHARACTERISTIC_NAMES: dict = {
    CHARACTERISTIC_MANUFACTURER_INFO : "CAMERA MANUFACTURER INFO(Read)",
    CHARACTERISTIC_MODEL_INFO:         "CAMERA MODEL INFO(Read)",
    CHARACTERISTIC_OUTGOING:           "OUTGOING_CONTROL (Write)",
    CHARACTERISTIC_INCOMING:           "INCOMING_CONTROL (Indicate)",
    CHARACTERISTIC_TIMECODE:           "TIMECODE (Notify)",
    CHARACTERISTIC_CAM_STATUS:         "CAMERA_STATUS (Notify)",
    CHARACTERISTIC_DEVICE_NAME:        "DEVICE_NAME (Read)",
    CHARACTERISTIC_PROTO_VER:          "PROTOCOL_VERSION (Read)",
    # CHARACTERISTIC_UNKNOWN_WRITE:      "UNKNOWN_WRITE (Write)",
}


# ─────────────────────────────────────────────────────────────────────────────
# Timing constants
# ─────────────────────────────────────────────────────────────────────────────

BLE_SCAN_TIMEOUT_S = 15
BLE_CONNECT_TIMEOUT_S     = 10.0