import asyncio
import logging
import struct

from bleak import BleakClient, BleakError, BleakGATTServiceCollection

from .camera_profile import CameraProfile
from .constants import (
    BLE_CONNECT_TIMEOUT_S,
    CHARACTERISTIC_CAM_STATUS,
    CHARACTERISTIC_INCOMING,
    CHARACTERISTIC_MANUFACTURER_INFO,
    CHARACTERISTIC_MODEL_INFO,
    CHARACTERISTIC_TIMECODE,
    GAP_CHARACTERISTIC_APPEARANCE,
    GAP_CHARACTERISTIC_DEVICE_NAME,
)
from .scanner import DiscoveredCamera, scan_for_camera

logger = logging.getLogger(__name__)


class BMDCameraController:
    """
    Async controller for one Blackmagic Design camera over BLE.
    """

    def __init__(self, discovered: DiscoveredCamera, profile: CameraProfile) -> None:
        self.discovered = discovered
        self._profile = profile

        self._client: BleakClient | None = None

        # GAP / Device info
        self.gap_device_name: str | None = None
        self.gap_appearance: int | None = None
        self.manufacturer_info: str | None = None
        self.model_info: str | None = None

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
            logger.info(f"No address — scanning for '{self.discovered.ble_name}' …")
            found = await scan_for_camera(self.discovered.ble_name)
            address = found.address
            # Update our discovered record with the resolved address
            self.discovered = DiscoveredCamera(
                address=address,
                ble_name=found.ble_name,
                rssi=found.rssi,
            )

        logger.info(f"Connecting to '{self.discovered.ble_name}' at {address} …")

        self._client = BleakClient(address)
        try:
            await asyncio.wait_for(self._client.connect(), timeout=BLE_CONNECT_TIMEOUT_S)
        except (TimeoutError, BleakError) as exc:
            raise RuntimeError(f"[{self.discovered.ble_name}] Connect failed: {exc}") from exc

        logger.info(f"Connected to {self.discovered.address} ({self.discovered.ble_name})")

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            for char in [
                CHARACTERISTIC_INCOMING,
                CHARACTERISTIC_CAM_STATUS,
                CHARACTERISTIC_TIMECODE,
            ]:
                try:
                    await self._client.stop_notify(char)
                except BleakError:
                    pass
                except KeyError:
                    # Notification was not active or already stopped
                    pass

            await self._client.disconnect()
            logger.info(f"Disconnected from {self.discovered.address}")

    # ── Services ────────────────────────────────────────────────────────────────────────

    async def get_services(self) -> BleakGATTServiceCollection:
        """
        Get GATT services in a way that works across Bleak versions.
        Some Bleak versions expose services via client.services after connection.
        Older versions may expose get_services().
        """
        if not self._client:
            raise RuntimeError(
                f"[{self.discovered.ble_name}] is not connected to a BLE client."
                f"Failed to get GATT services from {self.discovered.ble_name}"
            )
        services = getattr(self._client, "services", None)
        if services:
            return services
        get_services_method = getattr(self._client, "get_services", None)
        if get_services_method:
            return await get_services_method()
        raise RuntimeError("Unable to retrieve GATT services from BleakClient")

    # ── Device identity/Info ────────────────────────────────────────────────────────────

    @staticmethod
    def _decode_utf8_characteristic(value: bytes | None) -> str | None:
        """
        Decode a UTF-8/string BLE characteristic.

        BLE string characteristics may contain null padding and trailing whitespace.
        Invalid UTF-8 bytes are replaced instead of raising decode errors.
        Used for:
        - Device Name
        - Manufacturer Name
        - Model Number / Model Info
        """
        logger.debug(f"Decoding UTF-8/string BLE characteristic -> {value}")
        if value is None:
            return None

        return value.decode("utf-8", errors="replace").strip(" \t\r\n\x00")

    @staticmethod
    def _decode_appearance(value: bytes | None) -> int | None:
        """
        Decode GAP Appearance characteristic.
        BLE Appearance characteristic is a 16-bit unsigned integer,
        little-endian encoded.
        """
        logger.debug(f"Decoding GAP Appearance characteristic -> {value}")
        if value is None or len(value) < 2:
            return None
        return struct.unpack("<H", value[:2])[0]

    async def _read_metadata_characteristic(self, characteristic_uuid: str) -> bytes | None:
        """
        Read a BLE metadata characteristic safely.
        """
        if not self._client.is_connected:
            logger.warning(
                "Cannot read %s because BLE client is disconnected",
                characteristic_uuid,
            )
            return None
        try:
            value = await self._client.read_gatt_char(characteristic_uuid)
            logger.info("Read %s: %r", characteristic_uuid, value)
            return bytes(value)
        except (BleakError, OSError) as exc:
            logger.warning("Failed to read %s: %s", characteristic_uuid, exc)
            return None

    async def read_gap_identity_metadata(self) -> None:
        """
        Read metadata from the Generic Access Profile service.
        GAP reads are best-effort. Some camera models may disconnect or reject reads
        for GAP Device Name / Appearance.
        """
        if not self._profile.gap_metadata_readable:
            logger.info("Reading GAP metadata is not reliable for this device")
            return
        if self._client is None:
            raise RuntimeError("Camera is not connected")
        device_name_raw = await self._read_metadata_characteristic(GAP_CHARACTERISTIC_DEVICE_NAME)
        appearance_raw = await self._read_metadata_characteristic(GAP_CHARACTERISTIC_APPEARANCE)
        self.gap_device_name = self._decode_utf8_characteristic(device_name_raw)
        self.gap_appearance = self._decode_appearance(appearance_raw)

    async def read_device_information_metadata(self) -> None:
        """
        Read metadata from the standard Bluetooth Device Information Service.

        This reads the Manufacturer Name and Model Number characteristics from the
        Device Information Service.

        Expected values for Blackmagic cameras include:
        - Manufacturer Name: "Blackmagic Design"
        - Model Number / Model Info: camera model name

        Reads are best-effort. Some camera models may disconnect or reject reads for
        these standard characteristics. If the camera profile marks Device Information
        metadata as unreliable, the method returns without attempting GATT reads.

        If a characteristic is missing, unreadable, or the camera disconnects during
        the read, the corresponding controller attribute remains ``None``.
        """
        if not self._profile.device_info_metadata_readable:
            logger.info("Reading Device Info metadata is not reliable for this device")
            return
        if self._client is None:
            raise RuntimeError("Camera is not connected")
        manufacturer_raw = await self._read_metadata_characteristic(
            CHARACTERISTIC_MANUFACTURER_INFO
        )
        model_raw = await self._read_metadata_characteristic(CHARACTERISTIC_MODEL_INFO)
        self.manufacturer_info = self._decode_utf8_characteristic(manufacturer_raw)
        self.model_info = self._decode_utf8_characteristic(model_raw)
