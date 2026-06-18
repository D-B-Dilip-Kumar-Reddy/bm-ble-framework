import asyncio
import logging
import struct
from collections.abc import Callable
from typing import Any

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
    RECONNECT_DELAY_S,
    RECONNECT_MAX_ATTEMPTS,
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
        self._connected = asyncio.Event()
        self._intentional_disconnect: bool = False
        self._reconnecting: bool = False
        self._incoming_callback: Callable[[Any, bytearray], None] | None = None

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
        self._intentional_disconnect = False
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

        def on_disconnect(client: BleakClient) -> None:
            self._connected.clear()
            if self._intentional_disconnect:
                # Suppress reconnect — we initiated the disconnect ourselves.
                logger.info("Disconnected (intentional).")
                return
            if self._reconnecting:
                logger.debug("Reconnect already in progress — ignoring duplicate disconnect event.")
                return
            logger.warning("Disconnected unexpectedly!")
            asyncio.get_event_loop().create_task(self._reconnect_loop())

        logger.info(f"Connecting to '{self.discovered.ble_name}' at {address} …")

        self._client = BleakClient(address, disconnected_callback=on_disconnect)
        try:
            await asyncio.wait_for(self._client.connect(), timeout=BLE_CONNECT_TIMEOUT_S)
        except (TimeoutError, BleakError) as exc:
            raise RuntimeError(f"[{self.discovered.ble_name}] Connect failed: {exc}") from exc
        self._connected.set()
        logger.info(f"Connected to {self.discovered.address} ({self.discovered.ble_name})")

    async def disconnect(self) -> None:
        self._intentional_disconnect = True
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
            self._connected.clear()
            logger.info(f"Disconnected from {self.discovered.address}")

    # ── Reconnect ───────────────────────────────────────────────────────────────────────

    async def _reconnect_loop(self) -> None:
        self._reconnecting = True
        try:
            for attempt in range(1, RECONNECT_MAX_ATTEMPTS + 1):
                delay = RECONNECT_DELAY_S * attempt
                logger.warning(f"Reconnect {attempt}/{RECONNECT_MAX_ATTEMPTS} in {delay:.0f}s …")
                await asyncio.sleep(delay)

                # Camera may have auto-reconnected at OS level during the delay.
                if self._client and self._client.is_connected:
                    logger.info("Camera auto-reconnected — skipping explicit reconnect.")
                    self._connected.set()
                    return

                try:
                    await self.connect()
                    logger.info("Reconnected ✓")
                    if self._incoming_callback is not None:
                        await self.subscribe_incoming(callback=self._incoming_callback)
                    return
                except RuntimeError as exc:
                    logger.error(f"Reconnect attempt {attempt} failed: {exc}")

            logger.critical(
                f"All {RECONNECT_MAX_ATTEMPTS} reconnect attempts failed. Camera offline."
            )
        finally:
            self._reconnecting = False

    # ── Notifications ────────────────────────────────────────────────────────────────────

    async def subscribe_incoming(
        self,
        callback: Callable[[Any, bytearray], None] | None = None,
        retries: int = 3,
        retry_delay_s: float = 10.0,
    ) -> None:
        """
        Subscribe to INCOMING_CONTROL notifications.

        Must be called after connect(). If no callback is supplied the default
        handler logs every notification as uppercase hex pairs at DEBUG level,
        matching the sniffer output format for direct comparison.

        The callback signature follows Bleak 0.21+:
            callback(characteristic: BleakGATTCharacteristic, data: bytearray)

        Older camera firmware (e.g. PCC 6K G2 v7.9) needs a moment after connect
        before it accepts CCCD writes. ``retries`` and ``retry_delay_s`` handle
        that transparently — the default values cover the observed timing gap.
        """
        if self._client is None:
            raise RuntimeError(f"[{self.discovered.ble_name}] Cannot subscribe: not connected")
        if not self._client.is_connected:
            raise RuntimeError(
                f"[{self.discovered.ble_name}] Cannot subscribe: BLE client is disconnected"
            )
        handler = callback if callback is not None else self._log_incoming
        last_exc: BleakError | None = None
        for attempt in range(1, retries + 1):
            try:
                await self._client.start_notify(CHARACTERISTIC_INCOMING, handler)
                logger.info(
                    "[%s @ %s] Subscribed to INCOMING_CONTROL",
                    self.discovered.ble_name,
                    self.discovered.address,
                )
                self._incoming_callback = handler
                last_exc = None
                return
            except BleakError as exc:
                last_exc = exc
                if "not connected" in str(exc).lower():
                    raise RuntimeError(
                        f"[{self.discovered.ble_name}] start_notify failed for "
                        f"{CHARACTERISTIC_INCOMING}: connection lost mid-subscribe. {exc}"
                    ) from exc
                last_exc = exc
            except OSError as exc:
                last_exc = exc
            if last_exc and attempt < retries:
                logger.warning(
                    "[%s @ %s] start_notify attempt %d/%d failed (%s) — retrying in %.1f s",
                    self.discovered.ble_name,
                    self.discovered.address,
                    attempt,
                    retries,
                    last_exc,
                    retry_delay_s,
                )
                await asyncio.sleep(retry_delay_s)
        raise RuntimeError(
            f"[{self.discovered.ble_name}] Could not subscribe to INCOMING_CONTROL "
            f"after {retries} attempts: {last_exc}"
        ) from last_exc

    def _log_incoming(self, _characteristic: Any, data: bytearray) -> None:
        """Default INCOMING_CONTROL handler — logs raw bytes as uppercase hex."""
        logger.debug(
            "[%s @ %s] RX: %s",
            self.discovered.ble_name,
            self.discovered.address,
            " ".join(f"{b:02X}" for b in data),
        )

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
