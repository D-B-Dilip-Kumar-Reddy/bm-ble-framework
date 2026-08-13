"""
bmd_camera — Blackmagic Design Camera Control Framework (BLE, REST/WebSocket)
"""

from .ble.session import CameraSession
from .camera_profile import (
    KNOWN_PROFILES,
    CameraProfile,
    get_profile,
)
from .exceptions import BMDUnsupportedError, BMDVerificationError
from .rest.session import RestCameraSession

__all__ = [
    "BMDUnsupportedError",
    "BMDVerificationError",
    "CameraProfile",
    "CameraSession",
    "KNOWN_PROFILES",
    "RestCameraSession",
    "get_profile",
]
