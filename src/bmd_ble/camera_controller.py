import asyncio
import logging
from typing import Optional

from bleak import BleakClient, BleakError

from .constants import BLE_CONNECT_TIMEOUT_S, CHARACTERISTIC_INCOMING, \
    CHARACTERISTIC_CAM_STATUS, CHARACTERISTIC_TIMECODE
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

    # ── Connection ────────────────────────────────────────────────────────────

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