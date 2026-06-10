import asyncio
import logging
import struct
from typing import Optional, Dict, Any

from bleak import BleakClient, BleakError

from .constants import BLE_CONNECT_TIMEOUT_S, CHARACTERISTIC_INCOMING, \
    CHARACTERISTIC_CAM_STATUS, CHARACTERISTIC_TIMECODE, GAP_CHARACTERISTIC_DEVICE_NAME, \
    GAP_CHARACTERISTIC_APPEARANCE
from .scanner import DiscoveredCamera, scan_for_camera

logger = logging.getLogger(__name__)


class BMDCameraController:
    """
    Async controller for one Blackmagic Design camera over BLE.
    """

    def __init__(
        self,
        discovered: DiscoveredCamera
    ) -> None:
        self.discovered = discovered
        self._client: Optional[BleakClient] = None

    # ── Connection ──────────────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Connect to the camera.

        If ``discovered.address`` is empty (e.g. when constructed from a
        BLE name only), :func:`~scanner.scan_for_camera` is called first to
        resolve the address.
        """
        address = self.discovered.address
        if not address:
            logger.info(
                f"No address — scanning for '{self.discovered.ble_name}' …"
            )
            found = await scan_for_camera(
                self.discovered.ble_name
            )
            address = found.address
            # Update our discovered record with the resolved address
            self.discovered = DiscoveredCamera(
                address=address,
                ble_name=found.ble_name,
                rssi=found.rssi,
            )

        logger.info(
            f"Connecting to '{self.discovered.ble_name}' at {address} …"
        )

        self._client = BleakClient(address)
        try:
            await asyncio.wait_for(self._client.connect(),
                                   timeout=BLE_CONNECT_TIMEOUT_S)
        except (asyncio.TimeoutError, BleakError) as exc:
            raise RuntimeError(
                f"[{self.discovered.ble_name}] Connect failed: {exc}") from exc

        logger.info(
            f"Connected to {self.discovered.address} ({self.discovered.ble_name})"
        )

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            for char in [CHARACTERISTIC_INCOMING, CHARACTERISTIC_CAM_STATUS,
                         CHARACTERISTIC_TIMECODE]:
                try:
                    await self._client.stop_notify(char)
                except BleakError:
                    pass
                except KeyError:
                    # Notification was not active or already stopped
                    pass

            await self._client.disconnect()
        logger.info(
            f"Disconnected from {self.discovered.address}"
        )

    # ── Services ────────────────────────────────────────────────────────────────────────

    async def get_services(self) -> None:
        """
            Get GATT services in a way that works across Bleak versions.
            Some Bleak versions expose services via client.services after connection.
            Older versions may expose get_services().
        """
        if not self._client:
            raise RuntimeError(
                f"[{self.discovered.ble_name}] is not connected to a BLE client."
                f"Failed to get GATT services from {self.discovered.ble_name}")
        services = getattr(self._client, "services", None)
        if services:
            return services
        get_services_method = getattr(self._client, "get_services", None)
        if get_services_method:
            return await get_services_method()
        raise RuntimeError("Unable to retrieve GATT services from BleakClient")

    # ── Device identity/Info ────────────────────────────────────────────────────────────

    def _decode_utf8_characteristic(value: Optional[bytes]) -> Optional[str]:
        """
        Decode a UTF-8/string BLE characteristic.
        Used for:
        - Device Name
        - Manufacturer Name
        - Model Number / Model Info
        """
        if value is None:
            return None
        return value.decode("utf-8", errors="replace").strip("\x00").strip()

    def _decode_appearance(value: Optional[bytes]) -> Optional[int]:
        """
        Decode GAP Appearance characteristic.
        BLE Appearance characteristic is a 16-bit unsigned integer,
        little-endian encoded.
        """
        if value is None or len(value) < 2:
            return None
        return struct.unpack("<H", value[:2])[0]

    async def _read_metadata_characteristic(self, characteristic_uuid: str) -> Optional[
        bytes]:
        """
        Read a BLE metadata characteristic safely.
        Returns raw bytes if the characteristic is available,
        otherwise returns None.
        """
        try:
            return bytes(await self._client.read_gatt_char(characteristic_uuid))
        except Exception:
            return None

    async def read_gap_identity_metadata(self) -> Dict[str, Any]:
        """
        Read metadata from the Generic Access Profile service.
        Reads:
        - Device Name
        - Appearance
        """
        device_name_raw = await self._read_metadata_characteristic(
            GAP_CHARACTERISTIC_DEVICE_NAME
        )
        appearance_raw = await self._read_metadata_characteristic(
            GAP_CHARACTERISTIC_APPEARANCE
        )
        return {
            "device_name": self._decode_utf8_characteristic(device_name_raw),
            "appearance": self._decode_appearance(appearance_raw),
            "raw": {
                "device_name": device_name_raw,
                "appearance": appearance_raw,
            },
        }

