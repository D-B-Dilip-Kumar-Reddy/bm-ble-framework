"""Class-based unit tests for :mod:`bmd_ble.camera_controller`.

The tests in this module validate ``BMDCameraController`` without requiring real
Bluetooth hardware. Fake BLE clients are used to exercise connection handling,
disconnection cleanup, service discovery, GAP metadata reads, and decoder
helpers. The controller now receives a discovered camera and a profile object;
these tests verify that the profile object is retained instead of reloaded.
"""

from types import SimpleNamespace

import pytest
from bleak import BleakError

from bmd_ble.camera_controller import BMDCameraController
from bmd_ble.constants import (
    BLE_CONNECT_TIMEOUT_S,
    CHARACTERISTIC_CAM_STATUS,
    CHARACTERISTIC_INCOMING,
    CHARACTERISTIC_TIMECODE,
    GAP_CHARACTERISTIC_APPEARANCE,
    GAP_CHARACTERISTIC_DEVICE_NAME,
)
from bmd_ble.scanner import DiscoveredCamera

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v7.9"
ADDRESS = "AA:BB:CC:DD:EE:01"
BLE_NAME = "A:AF3DC814"


def make_profile() -> SimpleNamespace:
    """Return a minimal profile object accepted by ``BMDCameraController``."""
    return SimpleNamespace(
        model_key=MODEL_KEY,
        firmware=FIRMWARE,
        ble_name=BLE_NAME,
    )


def make_discovered(
    address: str = ADDRESS,
    ble_name: str = BLE_NAME,
    rssi: int | None = -45,
) -> DiscoveredCamera:
    """Return a discovered camera fixture with sensible defaults."""
    return DiscoveredCamera(address=address, ble_name=ble_name, rssi=rssi)


class FakeBleakClient:
    """Async fake for the subset of ``BleakClient`` used by the controller."""

    def __init__(self, address: str) -> None:
        """Create a fake BLE client for ``address`` with configurable behavior."""
        self.address = address
        self.is_connected = False
        self.connect_called = False
        self.disconnect_called = False
        self.services = None
        self.stopped_notifications: list[str] = []
        self.stop_notify_errors: dict[str, Exception] = {}
        self.read_values: dict[str, bytes | bytearray] = {}
        self.read_errors: dict[str, Exception] = {}
        self.read_calls: list[str] = []

    async def connect(self) -> None:
        """Simulate a successful BLE connection."""
        self.connect_called = True
        self.is_connected = True

    async def disconnect(self) -> None:
        """Simulate closing the BLE connection."""
        self.disconnect_called = True
        self.is_connected = False

    async def stop_notify(self, characteristic_uuid: str) -> None:
        """Record notification cleanup or raise a configured cleanup error."""
        error = self.stop_notify_errors.get(characteristic_uuid)
        if error is not None:
            raise error
        self.stopped_notifications.append(characteristic_uuid)

    async def read_gatt_char(self, characteristic_uuid: str) -> bytes | bytearray:
        """Return configured GATT bytes or raise a configured read error."""
        self.read_calls.append(characteristic_uuid)
        error = self.read_errors.get(characteristic_uuid)
        if error is not None:
            raise error
        return self.read_values[characteristic_uuid]


class FakeBleakClientWithGetServices(FakeBleakClient):
    """Fake client exposing an async ``get_services`` fallback method."""

    def __init__(self, address: str) -> None:
        """Create a fake client with fallback service discovery support."""
        super().__init__(address)
        self.fallback_services = object()
        self.get_services_called = False

    async def get_services(self) -> object:
        """Return fallback services and record that the fallback was used."""
        self.get_services_called = True
        return self.fallback_services


class FakeBleakClientWithoutServiceApi(FakeBleakClient):
    """Fake client without cached services or ``get_services`` support."""

    def __init__(self, address: str) -> None:
        """Create a fake client that cannot return GATT services."""
        super().__init__(address)
        del self.services


class TestBMDCameraControllerInitialization:
    """Construction and initial-state tests."""

    def test_initializes_with_discovered_camera_and_profile(self) -> None:
        """Controller should retain the discovered camera and supplied profile object."""
        discovered = make_discovered()
        profile = make_profile()

        controller = BMDCameraController(discovered=discovered, profile=profile)

        assert controller.discovered == discovered
        assert controller._profile is profile
        assert controller._client is None
        assert controller.gap_device_name is None
        assert controller.gap_appearance is None


class TestBMDCameraControllerConnect:
    """BLE connection-flow tests."""

    @pytest.mark.asyncio
    async def test_connect_uses_existing_address_without_scanning(self, monkeypatch) -> None:
        """A discovered camera with an address should connect without rescanning."""
        controller = BMDCameraController(make_discovered(), make_profile())

        async def fake_scan_for_camera(_ble_name: str) -> DiscoveredCamera:
            raise AssertionError("scan_for_camera should not be called")

        monkeypatch.setattr("bmd_ble.camera_controller.scan_for_camera", fake_scan_for_camera)
        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)

        await controller.connect()

        assert controller._client is not None
        assert controller._client.address == ADDRESS
        assert controller._client.connect_called is True
        assert controller._client.is_connected is True
        assert controller.discovered.address == ADDRESS

    @pytest.mark.asyncio
    async def test_connect_scans_when_address_is_missing(self, monkeypatch) -> None:
        """A discovered camera without an address should be resolved by BLE name."""
        scanned = make_discovered(address="AA:BB:CC:DD:EE:02", rssi=-58)
        controller = BMDCameraController(
            make_discovered(address="", rssi=None),
            make_profile(),
        )

        async def fake_scan_for_camera(ble_name: str) -> DiscoveredCamera:
            assert ble_name == BLE_NAME
            return scanned

        monkeypatch.setattr("bmd_ble.camera_controller.scan_for_camera", fake_scan_for_camera)
        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)

        await controller.connect()

        assert controller.discovered == scanned
        assert controller._client is not None
        assert controller._client.address == scanned.address
        assert controller._client.connect_called is True

    @pytest.mark.asyncio
    async def test_connect_uses_configured_timeout(self, monkeypatch) -> None:
        """Connection should use ``BLE_CONNECT_TIMEOUT_S`` with ``asyncio.wait_for``."""
        controller = BMDCameraController(make_discovered(), make_profile())
        captured_timeout = None

        async def fake_wait_for(coro, timeout):
            nonlocal captured_timeout
            captured_timeout = timeout
            return await coro

        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)
        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.wait_for", fake_wait_for)

        await controller.connect()

        assert captured_timeout == BLE_CONNECT_TIMEOUT_S

    @pytest.mark.asyncio
    async def test_connect_converts_timeout_to_runtime_error(self, monkeypatch) -> None:
        """Timeout failures should be wrapped in a camera-specific ``RuntimeError``."""
        controller = BMDCameraController(make_discovered(), make_profile())

        async def fake_wait_for(coro, timeout):
            coro.close()
            raise TimeoutError("timed out")

        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)
        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.wait_for", fake_wait_for)

        with pytest.raises(RuntimeError, match=rf"\[{BLE_NAME}\] Connect failed"):
            await controller.connect()

    @pytest.mark.asyncio
    async def test_connect_converts_bleak_error_to_runtime_error(self, monkeypatch) -> None:
        """Bleak connection failures should be wrapped in ``RuntimeError``."""
        controller = BMDCameraController(make_discovered(), make_profile())

        async def fake_wait_for(coro, timeout):
            coro.close()
            raise BleakError("adapter failed")

        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)
        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.wait_for", fake_wait_for)

        with pytest.raises(RuntimeError, match=rf"\[{BLE_NAME}\] Connect failed"):
            await controller.connect()


class TestBMDCameraControllerDisconnect:
    """Disconnect and notification-cleanup tests."""

    @pytest.mark.asyncio
    async def test_disconnect_stops_notifications_before_disconnect(self) -> None:
        """Disconnect should stop known notification characteristics before closing."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        controller._client = client

        await controller.disconnect()

        assert client.stopped_notifications == [
            CHARACTERISTIC_INCOMING,
            CHARACTERISTIC_CAM_STATUS,
            CHARACTERISTIC_TIMECODE,
        ]
        assert client.disconnect_called is True
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_disconnect_ignores_expected_stop_notify_errors(self) -> None:
        """Cleanup should ignore inactive-notification ``KeyError`` and ``BleakError``."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        client.stop_notify_errors = {
            CHARACTERISTIC_INCOMING: BleakError("not notifying"),
            CHARACTERISTIC_CAM_STATUS: KeyError("not active"),
        }
        controller._client = client

        await controller.disconnect()

        assert client.stopped_notifications == [CHARACTERISTIC_TIMECODE]
        assert client.disconnect_called is True

    @pytest.mark.asyncio
    async def test_disconnect_is_safe_without_client(self) -> None:
        """Calling ``disconnect`` before ``connect`` should not raise."""
        controller = BMDCameraController(make_discovered(), make_profile())

        await controller.disconnect()

        assert controller._client is None

    @pytest.mark.asyncio
    async def test_disconnect_is_safe_when_client_is_already_disconnected(self) -> None:
        """Already-disconnected clients should not receive cleanup calls."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = False
        controller._client = client

        await controller.disconnect()

        assert client.stopped_notifications == []
        assert client.disconnect_called is False


class TestBMDCameraControllerServices:
    """GATT service retrieval tests."""

    @pytest.mark.asyncio
    async def test_get_services_returns_cached_services(self) -> None:
        """Cached ``client.services`` should be returned when available."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.services = object()
        controller._client = client

        services = await controller.get_services()

        assert services is client.services

    @pytest.mark.asyncio
    async def test_get_services_uses_get_services_fallback(self) -> None:
        """Async ``get_services`` should be used when cached services are missing."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClientWithGetServices(ADDRESS)
        controller._client = client

        services = await controller.get_services()

        assert services is client.fallback_services
        assert client.get_services_called is True

    @pytest.mark.asyncio
    async def test_get_services_raises_without_client(self) -> None:
        """Service retrieval before client creation should raise ``RuntimeError``."""
        controller = BMDCameraController(make_discovered(), make_profile())

        with pytest.raises(RuntimeError, match="is not connected to a BLE client"):
            await controller.get_services()

    @pytest.mark.asyncio
    async def test_get_services_raises_when_client_has_no_service_api(self) -> None:
        """A client without service access should raise a clear error."""
        controller = BMDCameraController(make_discovered(), make_profile())
        controller._client = FakeBleakClientWithoutServiceApi(ADDRESS)

        with pytest.raises(RuntimeError, match="Unable to retrieve GATT services"):
            await controller.get_services()


class TestBMDCameraControllerDecoders:
    """Tests for pure GAP decoder helpers."""

    def test_decode_utf8_characteristic_strips_null_padding_and_whitespace(self) -> None:
        """UTF-8 characteristic values should strip null padding and whitespace."""
        result = BMDCameraController._decode_utf8_characteristic(b"A:026881AD\x00\x00  ")

        assert result == "A:026881AD"

    def test_decode_utf8_characteristic_returns_none_for_none(self) -> None:
        """Missing UTF-8 characteristic values should decode to ``None``."""
        assert BMDCameraController._decode_utf8_characteristic(None) is None

    def test_decode_utf8_characteristic_replaces_invalid_utf8_bytes(self) -> None:
        """Invalid UTF-8 bytes should be replaced instead of raising."""
        assert BMDCameraController._decode_utf8_characteristic(b"Camera\xff") == "Camera�"

    def test_decode_appearance_decodes_little_endian_uint16(self) -> None:
        """GAP Appearance bytes should decode as little-endian unsigned 16-bit."""
        assert BMDCameraController._decode_appearance(b"\x03\x84") == 33795

    @pytest.mark.parametrize("value", [None, b"", b"\x03"])
    def test_decode_appearance_returns_none_for_missing_or_short_value(
        self,
        value: bytes | None,
    ) -> None:
        """Missing or incomplete GAP Appearance values should decode to ``None``."""
        assert BMDCameraController._decode_appearance(value) is None


class TestBMDCameraControllerMetadata:
    """Safe metadata-read and GAP aggregation tests."""

    @pytest.mark.asyncio
    async def test_read_metadata_characteristic_raises_without_client(self) -> None:
        """Reading metadata before client creation should raise ``RuntimeError``."""
        controller = BMDCameraController(make_discovered(), make_profile())

        with pytest.raises(RuntimeError, match="Camera is not connected"):
            await controller._read_metadata_characteristic(GAP_CHARACTERISTIC_DEVICE_NAME)

    @pytest.mark.asyncio
    async def test_read_metadata_characteristic_returns_none_when_disconnected(self) -> None:
        """Disconnected clients should not attempt a GATT read."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = False
        controller._client = client

        result = await controller._read_metadata_characteristic(GAP_CHARACTERISTIC_DEVICE_NAME)

        assert result is None
        assert client.read_calls == []

    @pytest.mark.asyncio
    async def test_read_metadata_characteristic_returns_bytes(self) -> None:
        """Successful GATT reads should be normalized to immutable ``bytes``."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        client.read_values[GAP_CHARACTERISTIC_DEVICE_NAME] = bytearray(b"A:026881AD")
        controller._client = client

        result = await controller._read_metadata_characteristic(GAP_CHARACTERISTIC_DEVICE_NAME)

        assert result == b"A:026881AD"
        assert isinstance(result, bytes)
        assert client.read_calls == [GAP_CHARACTERISTIC_DEVICE_NAME]

    @pytest.mark.asyncio
    async def test_read_metadata_characteristic_returns_none_on_read_error(self) -> None:
        """GATT read exceptions should be treated as best-effort metadata failures."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        client.read_errors[GAP_CHARACTERISTIC_DEVICE_NAME] = RuntimeError("unreachable")
        controller._client = client

        result = await controller._read_metadata_characteristic(GAP_CHARACTERISTIC_DEVICE_NAME)

        assert result is None
        assert client.read_calls == [GAP_CHARACTERISTIC_DEVICE_NAME]

    @pytest.mark.asyncio
    async def test_read_gap_identity_metadata_sets_gap_attributes(self) -> None:
        """Readable Device Name and Appearance should populate controller attributes."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        client.read_values = {
            GAP_CHARACTERISTIC_DEVICE_NAME: bytearray(b"A:026881AD\x00\x00"),
            GAP_CHARACTERISTIC_APPEARANCE: bytearray(b"\x03\x84"),
        }
        controller._client = client

        result = await controller.read_gap_identity_metadata()

        assert result is None
        assert controller.gap_device_name == "A:026881AD"
        assert controller.gap_appearance == 33795
        assert client.read_calls == [
            GAP_CHARACTERISTIC_DEVICE_NAME,
            GAP_CHARACTERISTIC_APPEARANCE,
        ]

    @pytest.mark.asyncio
    async def test_read_gap_identity_metadata_skips_appearance_after_disconnect(
        self,
    ) -> None:
        """Appearance should not be read if Device Name read disconnects the client."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        controller._client = client

        async def fake_device_name_read(characteristic_uuid: str) -> None:
            client.read_calls.append(characteristic_uuid)
            client.is_connected = False
            return None

        controller._read_metadata_characteristic = fake_device_name_read

        result = await controller.read_gap_identity_metadata()

        assert result is None
        assert controller.gap_device_name is None
        assert controller.gap_appearance is None
        assert client.read_calls == [GAP_CHARACTERISTIC_DEVICE_NAME]

    @pytest.mark.asyncio
    async def test_read_gap_identity_metadata_raises_without_client(self) -> None:
        """GAP metadata reads should require an initialized BLE client."""
        controller = BMDCameraController(make_discovered(), make_profile())

        with pytest.raises(RuntimeError, match="Camera is not connected"):
            await controller.read_gap_identity_metadata()

    @pytest.mark.asyncio
    async def test_read_gap_identity_metadata_raises_when_client_is_disconnected(self) -> None:
        """GAP metadata reads should require an active BLE connection."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = False
        controller._client = client

        with pytest.raises(RuntimeError, match="Camera BLE client is disconnected"):
            await controller.read_gap_identity_metadata()
