"""
src/scanner.py
==============
Dynamic BLE camera discovery by BLE advertisement name.

The framework connects to cameras by the name visible on the camera screen
(BLE advertisement name), not by MAC address.  The name is stored in each
model JSON under ``_meta.ble_name`` and is used by the automation runner,
all targeted sniffers, and the functional tests.
"""

import asyncio
import logging
from dataclasses import dataclass

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .constants import BLE_SCAN_TIMEOUT_S

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredCamera:
    """
    Result of a successful BLE camera scan (or a manually constructed entry).
    Attributes:
        address:   BLE MAC address (Linux/Windows) or CoreBluetooth UUID (macOS).
                   May be empty string when constructed without a scan — the
                   controller will scan to resolve it at connect time.
        ble_name:  BLE advertisement name as seen on the camera screen.
        rssi:      Received signal strength in dBm.  ``None`` if not available.
    """

    address: str
    ble_name: str
    rssi: int | None = None


async def scan_for_camera(
    ble_name: str,
    timeout: float = BLE_SCAN_TIMEOUT_S,
):

    query = ble_name.lower().strip()
    found: list[tuple[BLEDevice, AdvertisementData]] = []

    logger.info("Scanning for '%s' (timeout=%.0fs) …", ble_name, timeout)

    def detection_callback(device: BLEDevice, adv: AdvertisementData) -> None:
        name = (device.name or "").lower()
        if query not in name:
            return
        logger.debug(
            "Candidate: '%s' at %s  RSSI=%s",
            device.name,
            device.address,
            adv.rssi,
        )
        found.append((device, adv))

    async with BleakScanner(detection_callback=detection_callback):
        await asyncio.sleep(timeout)

    if not found:
        raise RuntimeError(
            f"No camera matching '{ble_name}' found after {timeout}s. "
            f"Ensure the camera is on, Bluetooth is enabled "
            f"(Menu → Setup → Bluetooth → On), and is not already "
            f"connected to another BLE client. "
        )

    # Strongest signal wins
    found.sort(key=lambda x: x[1].rssi or -999, reverse=True)
    device, adv = found[0]

    logger.info(
        "Selected: '%s' | address=%s | RSSI=%s",
        device.name,
        device.address,
        adv.rssi,
    )
    return DiscoveredCamera(
        address=device.address,
        ble_name=device.name or ble_name,
        rssi=adv.rssi,
    )
