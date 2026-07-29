"""Class-based unit tests for :mod:`bmd_ble.camera_controller`.

The tests in this module validate ``BMDCameraController`` without requiring real
Bluetooth hardware. Fake BLE clients are used to exercise connection handling,
disconnection cleanup, service discovery, GAP metadata reads, Device
Information Service metadata reads, and decoder helpers.

The controller receives both a runtime ``DiscoveredCamera`` object and a static
camera profile object. The profile controls model-specific behavior, including
whether GAP and Device Information metadata reads are considered reliable for a
specific camera/firmware combination.
"""

from types import SimpleNamespace

import pytest
from bleak import BleakError

from bmd_ble.camera_controller import BMDCameraController
from bmd_ble.constants import (
    BLE_CONNECT_TIMEOUT_S,
    CHARACTERISTIC_CAM_STATUS,
    CHARACTERISTIC_INCOMING,
    CHARACTERISTIC_MANUFACTURER_INFO,
    CHARACTERISTIC_MODEL_INFO,
    CHARACTERISTIC_OUTGOING,
    CHARACTERISTIC_TIMECODE,
    GAP_CHARACTERISTIC_APPEARANCE,
    GAP_CHARACTERISTIC_DEVICE_NAME,
)
from bmd_ble.scanner import DiscoveredCamera

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v8.6"
ADDRESS = "AA:BB:CC:DD:EE:01"
BLE_NAME = "A:AF3DC814"
MANUFACTURER_NAME = "Blackmagic Design"
MODEL_NAME = "Blackmagic Pocket Cinema Camera 6K G2"


def make_profile(
    gap_metadata_readable: bool = True,
    device_info_metadata_readable: bool = True,
) -> SimpleNamespace:
    """Return a minimal camera profile object used by controller tests.

    The controller only needs the profile object to expose the attributes used
    by the current implementation. ``SimpleNamespace`` keeps these tests
    independent from profile file loading and verifies that the controller uses
    the supplied profile instead of loading the profile again.
    """
    return SimpleNamespace(
        model_key=MODEL_KEY,
        firmware=FIRMWARE,
        ble_name=BLE_NAME,
        gap_metadata_readable=gap_metadata_readable,
        device_info_metadata_readable=device_info_metadata_readable,
    )


def make_discovered(
    address: str = ADDRESS,
    ble_name: str = BLE_NAME,
    rssi: int | None = -45,
) -> DiscoveredCamera:
    """Return a discovered camera test fixture with sensible defaults."""
    return DiscoveredCamera(address=address, ble_name=ble_name, rssi=rssi)


class FakeConnectedBleakClient:
    """Fake client that is already connected and records start_notify calls."""

    is_connected = True

    def __init__(self, address: str) -> None:
        self.address = address
        self.notified: dict[str, object] = {}

    async def connect(self) -> None:
        pass

    async def start_notify(self, uuid: str, callback: object) -> None:
        self.notified[uuid] = callback


class FakeBleakClient:
    """Async fake for the subset of ``BleakClient`` used by the controller."""

    def __init__(self, address: str, disconnected_callback=None) -> None:
        """Create a fake BLE client for ``address`` with configurable behavior."""
        self.address = address
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.connect_called = False
        self.disconnect_called = False
        self.services = None
        self.stopped_notifications: list[str] = []
        self.stop_notify_errors: dict[str, Exception] = {}
        self.read_values: dict[str, bytes | bytearray] = {}
        self.read_errors: dict[str, Exception] = {}
        self.read_calls: list[str] = []
        self.write_errors: dict[str, Exception] = {}
        self.write_calls: list[tuple[str, bytes]] = []
        self.notified: dict[str, object] = {}

    async def connect(self) -> None:
        """Simulate a successful BLE connection."""
        self.connect_called = True
        self.is_connected = True

    async def disconnect(self) -> None:
        """Simulate closing the BLE connection."""
        self.disconnect_called = True
        self.is_connected = False

    async def start_notify(self, uuid: str, callback: object) -> None:
        """Record notification subscription."""
        self.notified[uuid] = callback

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

    async def write_gatt_char(self, characteristic_uuid: str, data: bytes) -> None:
        """Record a write call or raise a configured write error."""
        error = self.write_errors.get(characteristic_uuid)
        if error is not None:
            raise error
        self.write_calls.append((characteristic_uuid, bytes(data)))


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
        """Controller should retain the discovered camera and supplied profile."""
        discovered = make_discovered()
        profile = make_profile()

        controller = BMDCameraController(discovered=discovered, profile=profile)

        assert controller.discovered == discovered
        assert controller._profile is profile
        assert controller._client is None
        assert controller.gap_device_name is None
        assert controller.gap_appearance is None

    def test_initializes_device_information_attributes_to_none(self) -> None:
        """Device Information metadata attributes should start unset."""
        controller = BMDCameraController(make_discovered(), make_profile())

        assert controller.manufacturer_info is None
        assert controller.model_info is None

    def test_initializes_connected_event_cleared(self) -> None:
        """``_connected`` event must start in the cleared (unset) state."""
        controller = BMDCameraController(make_discovered(), make_profile())

        assert controller._connected.is_set() is False

    def test_initializes_intentional_disconnect_false(self) -> None:
        """``_intentional_disconnect`` must start as False."""
        controller = BMDCameraController(make_discovered(), make_profile())

        assert controller._intentional_disconnect is False

    def test_initializes_reconnecting_false(self) -> None:
        """``_reconnecting`` must start as False."""
        controller = BMDCameraController(make_discovered(), make_profile())

        assert controller._reconnecting is False

    def test_initializes_incoming_callback_none(self) -> None:
        """``_incoming_callback`` must start as None (no subscription yet)."""
        controller = BMDCameraController(make_discovered(), make_profile())

        assert controller._incoming_callback is None

    def test_initializes_conn_gen_zero(self) -> None:
        """Connection generation counter must start at zero (no connection yet)."""
        controller = BMDCameraController(make_discovered(), make_profile())

        assert controller._conn_gen == 0

    def test_initializes_connect_lock(self) -> None:
        """``_connect_lock`` must be an asyncio.Lock instance."""
        import asyncio as _asyncio

        controller = BMDCameraController(make_discovered(), make_profile())

        assert isinstance(controller._connect_lock, _asyncio.Lock)

    def test_initializes_last_rx_time_none(self) -> None:
        """``_last_rx_time`` must start as None — no notifications received yet."""
        controller = BMDCameraController(make_discovered(), make_profile())

        assert controller._last_rx_time is None


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
            del timeout
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
            del timeout
            coro.close()
            raise BleakError("adapter failed")

        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)
        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.wait_for", fake_wait_for)

        with pytest.raises(RuntimeError, match=rf"\[{BLE_NAME}\] Connect failed"):
            await controller.connect()

    @pytest.mark.asyncio
    async def test_connect_passes_disconnect_callback_to_bleak_client(self, monkeypatch) -> None:
        """``connect()`` must pass a ``disconnected_callback`` to ``BleakClient``."""
        controller = BMDCameraController(make_discovered(), make_profile())
        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)

        await controller.connect()

        assert controller._client.disconnected_callback is not None

    @pytest.mark.asyncio
    async def test_connect_sets_connected_event_on_success(self, monkeypatch) -> None:
        """After a successful ``connect()``, the ``_connected`` event must be set."""
        controller = BMDCameraController(make_discovered(), make_profile())
        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)

        await controller.connect()

        assert controller._connected.is_set() is True

    @pytest.mark.asyncio
    async def test_connect_resets_intentional_disconnect_flag(self, monkeypatch) -> None:
        """``connect()`` must reset ``_intentional_disconnect`` so future drops trigger reconnect."""  # noqa: E501
        controller = BMDCameraController(make_discovered(), make_profile())
        controller._intentional_disconnect = True
        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)

        await controller.connect()

        assert controller._intentional_disconnect is False

    @pytest.mark.asyncio
    async def test_connect_increments_conn_gen(self, monkeypatch) -> None:
        """``_conn_gen`` must increase by 1 on every successful ``connect()``."""
        controller = BMDCameraController(make_discovered(), make_profile())
        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)

        assert controller._conn_gen == 0
        await controller.connect()
        assert controller._conn_gen == 1

    @pytest.mark.asyncio
    async def test_connect_nulls_client_on_connection_failure(self, monkeypatch) -> None:
        """If ``BleakClient.connect()`` raises, ``_client`` must be reset to None."""
        from bleak import BleakError

        class FailingBleakClient(FakeBleakClient):
            async def connect(self) -> None:
                raise BleakError("device not found")

        controller = BMDCameraController(make_discovered(), make_profile())
        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FailingBleakClient)

        with pytest.raises(RuntimeError):
            await controller.connect()

        assert controller._client is None

    @pytest.mark.asyncio
    async def test_connect_is_idempotent_if_already_connected(self, monkeypatch) -> None:
        """A second ``connect()`` call must be a no-op when the camera is already connected."""
        controller = BMDCameraController(make_discovered(), make_profile())
        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)

        await controller.connect()
        assert controller._conn_gen == 1

        controller._client.connect_called = False
        await controller.connect()

        assert controller._client.connect_called is False
        assert controller._conn_gen == 1


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

    @pytest.mark.asyncio
    async def test_disconnect_sets_intentional_disconnect_flag(self) -> None:
        """``disconnect()`` must set ``_intentional_disconnect`` to prevent reconnect loop."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        controller._client = client

        await controller.disconnect()

        assert controller._intentional_disconnect is True

    @pytest.mark.asyncio
    async def test_disconnect_clears_connected_event(self) -> None:
        """``disconnect()`` must clear the ``_connected`` event."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        controller._client = client
        controller._connected.set()

        await controller.disconnect()

        assert controller._connected.is_set() is False


class TestBMDCameraControllerReconnectLoop:
    """_reconnect_loop behaviour tests."""

    @pytest.mark.asyncio
    async def test_reconnect_loop_exits_early_when_client_auto_reconnects(
        self, monkeypatch
    ) -> None:
        """If the camera auto-reconnects at OS level, the loop must not call connect()."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        controller._client = client
        connect_called = False

        async def fake_connect():
            nonlocal connect_called
            connect_called = True

        async def fake_sleep(_):
            pass

        monkeypatch.setattr(controller, "connect", fake_connect)
        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)

        await controller._reconnect_loop()

        assert connect_called is False
        assert controller._connected.is_set() is True

    @pytest.mark.asyncio
    async def test_reconnect_loop_clears_reconnecting_flag_on_auto_reconnect(
        self, monkeypatch
    ) -> None:
        """``_reconnecting`` must be False when the loop exits via auto-reconnect."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        controller._client = client

        async def fake_sleep(_):
            pass

        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)

        await controller._reconnect_loop()

        assert controller._reconnecting is False

    @pytest.mark.asyncio
    async def test_reconnect_loop_clears_reconnecting_flag_on_all_failures(
        self, monkeypatch, caplog
    ) -> None:
        """``_reconnecting`` must be False even when all reconnect attempts fail."""
        import logging

        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = False
        controller._client = client

        async def failing_connect():
            raise RuntimeError("connect failed")

        async def fake_sleep(_):
            pass

        monkeypatch.setattr(controller, "connect", failing_connect)
        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)

        with caplog.at_level(logging.CRITICAL, logger="bmd_ble.camera_controller"):
            await controller._reconnect_loop()

        assert controller._reconnecting is False
        assert "Camera offline" in caplog.text

    @pytest.mark.asyncio
    async def test_reconnect_loop_resubscribes_if_previously_subscribed(self, monkeypatch) -> None:
        """After an explicit reconnect, stored callbacks are used for resubscription.

        Subscriptions are restored inside connect() → subscribe_all(), so the real
        connect() must run. BleakClient is patched to a fake; subscribe_incoming is
        patched to capture which callback is passed.
        """
        controller = BMDCameraController(make_discovered(), make_profile())
        stale = FakeBleakClient(ADDRESS)
        stale.is_connected = False
        controller._client = stale

        def my_callback(_char, _data):
            pass

        controller._incoming_callback = my_callback
        subscribe_calls: list = []

        async def fake_subscribe_incoming(callback=None, **kwargs):
            subscribe_calls.append(callback)

        async def fake_sleep(_):
            pass

        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)
        monkeypatch.setattr(controller, "subscribe_incoming", fake_subscribe_incoming)
        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)

        await controller._reconnect_loop()

        assert subscribe_calls == [my_callback]

    @pytest.mark.asyncio
    async def test_on_disconnect_ignores_duplicate_when_reconnecting(self, monkeypatch) -> None:
        """``on_disconnect`` must not schedule a second loop if one is already running."""
        controller = BMDCameraController(make_discovered(), make_profile())
        monkeypatch.setattr("bmd_ble.camera_controller.BleakClient", FakeBleakClient)
        await controller.connect()

        task_count = 0

        def counting_create_task(coro):
            nonlocal task_count
            task_count += 1
            coro.close()

        import asyncio as _asyncio

        fake_loop = type("L", (), {"create_task": staticmethod(counting_create_task)})()
        monkeypatch.setattr(_asyncio, "get_event_loop", lambda: fake_loop)

        controller._reconnecting = True
        controller._client.disconnected_callback(controller._client)

        assert task_count == 0

    @pytest.mark.asyncio
    async def test_reconnect_loop_pre_delay_exits_if_stale_client_already_connected(
        self, monkeypatch
    ) -> None:
        """Pre-delay check: if stale client is already connected, loop exits before sleeping."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        controller._client = client
        sleep_called = False

        async def fake_sleep(_):
            nonlocal sleep_called
            sleep_called = True

        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)

        await controller._reconnect_loop()

        assert sleep_called is False
        assert controller._connected.is_set() is True

    @pytest.mark.asyncio
    async def test_reconnect_loop_calls_stop_notify_on_stale_client_before_explicit_connect(
        self, monkeypatch
    ) -> None:
        """stale.stop_notify must be called for tracked characteristics before explicit connect."""
        controller = BMDCameraController(make_discovered(), make_profile())
        stale = FakeBleakClient(ADDRESS)
        stale.is_connected = False
        controller._client = stale

        async def fake_sleep(_):
            pass

        async def fake_connect():
            controller._connected.set()
            stale.is_connected = True

        async def fake_subscribe_all():
            pass

        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)
        monkeypatch.setattr(controller, "connect", fake_connect)
        monkeypatch.setattr(controller, "subscribe_all", fake_subscribe_all)

        await controller._reconnect_loop()

        assert CHARACTERISTIC_INCOMING in stale.stopped_notifications

    @pytest.mark.asyncio
    async def test_reconnect_loop_aborts_pre_delay_on_rx_activity(self, monkeypatch) -> None:
        """If recent RX activity exists before the delay, loop exits without sleeping."""
        import time

        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = False
        controller._client = client
        controller._last_rx_time = time.monotonic()  # RX was just received
        sleep_called = False
        connect_called = False

        async def fake_sleep(_):
            nonlocal sleep_called
            sleep_called = True

        async def fake_connect():
            nonlocal connect_called
            connect_called = True

        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)
        monkeypatch.setattr(controller, "connect", fake_connect)

        await controller._reconnect_loop()

        assert sleep_called is False
        assert connect_called is False
        assert controller._connected.is_set() is True

    @pytest.mark.asyncio
    async def test_reconnect_loop_aborts_post_delay_on_rx_activity(self, monkeypatch) -> None:
        """RX arriving during the sleep delay causes the loop to abort after waking."""
        import time

        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = False
        controller._client = client
        connect_called = False

        async def fake_sleep(_):
            # Simulate notifications arriving while we were sleeping.
            controller._last_rx_time = time.monotonic()

        async def fake_connect():
            nonlocal connect_called
            connect_called = True

        monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)
        monkeypatch.setattr(controller, "connect", fake_connect)

        await controller._reconnect_loop()

        assert connect_called is False
        assert controller._connected.is_set() is True


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
    """Tests for pure BLE metadata decoder helpers."""

    def test_decode_utf8_characteristic_strips_null_padding(self) -> None:
        """UTF-8 characteristic values should strip trailing null padding."""
        result = BMDCameraController._decode_utf8_characteristic(b"A:026881AD\x00\x00")

        assert result == "A:026881AD"

    def test_decode_utf8_characteristic_strips_surrounding_whitespace(self) -> None:
        """UTF-8 characteristic values should strip surrounding whitespace."""
        result = BMDCameraController._decode_utf8_characteristic(b"  A:026881AD  ")

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


class TestBMDCameraControllerGapMetadata:
    """Safe GAP metadata-read and aggregation tests."""

    @pytest.mark.asyncio
    async def test_read_gap_identity_metadata_returns_early_when_profile_disables_gap_reads(
        self,
    ) -> None:
        """Profiles with unreliable GAP reads should skip client and GATT access."""
        controller = BMDCameraController(
            make_discovered(),
            make_profile(gap_metadata_readable=False),
        )

        result = await controller.read_gap_identity_metadata()

        assert result is None
        assert controller._client is None
        assert controller.gap_device_name is None
        assert controller.gap_appearance is None

    @pytest.mark.asyncio
    async def test_read_gap_identity_metadata_does_not_read_when_profile_disables_gap_reads(
        self,
    ) -> None:
        """Disabled GAP reads should not call ``read_gatt_char`` even with a client."""
        controller = BMDCameraController(
            make_discovered(),
            make_profile(gap_metadata_readable=False),
        )
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        controller._client = client

        await controller.read_gap_identity_metadata()

        assert client.read_calls == []
        assert controller.gap_device_name is None
        assert controller.gap_appearance is None

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
    async def test_read_gap_identity_metadata_attempts_appearance_after_disconnect(
        self,
    ) -> None:
        """Appearance is still attempted if Device Name read disconnects the client."""
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
        assert client.read_calls == [
            GAP_CHARACTERISTIC_DEVICE_NAME,
            GAP_CHARACTERISTIC_APPEARANCE,
        ]

    @pytest.mark.asyncio
    async def test_read_gap_identity_metadata_raises_without_client_when_profile_allows_reads(
        self,
    ) -> None:
        """Enabled GAP reads should require an initialized BLE client."""
        controller = BMDCameraController(make_discovered(), make_profile())

        with pytest.raises(RuntimeError, match="Camera is not connected"):
            await controller.read_gap_identity_metadata()

    @pytest.mark.asyncio
    async def test_read_gap_identity_metadata_returns_none_when_client_is_disconnected(
        self,
    ) -> None:
        """Enabled GAP reads should return without values when the BLE client is disconnected."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = False
        controller._client = client

        result = await controller.read_gap_identity_metadata()

        assert result is None
        assert controller.gap_device_name is None
        assert controller.gap_appearance is None
        assert client.read_calls == []


class TestBMDCameraControllerMetadataCharacteristicReads:
    """Tests for the shared best-effort characteristic read helper."""

    @pytest.mark.asyncio
    async def test_read_metadata_characteristic_raises_attribute_error_without_client(self) -> None:
        """Reading metadata before client creation raises from accessing the missing client."""
        controller = BMDCameraController(make_discovered(), make_profile())

        with pytest.raises(
            AttributeError, match="'NoneType' object has no attribute 'is_connected'"
        ):
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
    async def test_read_metadata_characteristic_propagates_unhandled_read_error(self) -> None:
        """Unhandled GATT read exceptions should propagate from the metadata helper."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        client.read_errors[GAP_CHARACTERISTIC_DEVICE_NAME] = RuntimeError("unreachable")
        controller._client = client

        with pytest.raises(RuntimeError, match="unreachable"):
            await controller._read_metadata_characteristic(GAP_CHARACTERISTIC_DEVICE_NAME)

        assert client.read_calls == [GAP_CHARACTERISTIC_DEVICE_NAME]


class TestWriteOutgoingControl:
    """Tests for the raw OUTGOING_CONTROL write transport method."""

    @pytest.mark.asyncio
    async def test_write_outgoing_control_raises_without_client(self) -> None:
        """Writing before a client exists raises, rather than attempting a write."""
        controller = BMDCameraController(make_discovered(), make_profile())

        with pytest.raises(RuntimeError, match="Cannot write"):
            await controller.write_outgoing_control(b"\x01")

    @pytest.mark.asyncio
    async def test_write_outgoing_control_raises_when_disconnected(self) -> None:
        """A disconnected client should not attempt a GATT write."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = False
        controller._client = client

        with pytest.raises(RuntimeError, match="Cannot write"):
            await controller.write_outgoing_control(b"\x01")

        assert client.write_calls == []

    @pytest.mark.asyncio
    async def test_write_outgoing_control_writes_to_outgoing_characteristic(self) -> None:
        """A connected client writes the given bytes to CHARACTERISTIC_OUTGOING."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        controller._client = client

        packet = bytes([0xFF, 0x05, 0x00, 0x01, 0x0A, 0x01, 0x01, 0x00, 0x02])
        await controller.write_outgoing_control(packet)

        assert client.write_calls == [(CHARACTERISTIC_OUTGOING, packet)]

    @pytest.mark.asyncio
    async def test_write_outgoing_control_propagates_unhandled_write_error(self) -> None:
        """Unhandled GATT write exceptions should propagate to the caller."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        client.write_errors[CHARACTERISTIC_OUTGOING] = BleakError("write failed")
        controller._client = client

        with pytest.raises(BleakError, match="write failed"):
            await controller.write_outgoing_control(b"\x01")


class TestBMDCameraControllerDeviceInformationMetadata:
    """Device Information Service metadata-read tests."""

    @pytest.mark.asyncio
    async def test_read_device_information_metadata_returns_early_when_profile_disables_reads(
        self,
    ) -> None:
        """Profiles with unreliable Device Info reads should skip all GATT access."""
        controller = BMDCameraController(
            make_discovered(),
            make_profile(device_info_metadata_readable=False),
        )

        result = await controller.read_device_information_metadata()

        assert result is None
        assert controller._client is None
        assert controller.manufacturer_info is None
        assert controller.model_info is None

    @pytest.mark.asyncio
    async def test_read_device_information_metadata_does_not_require_client_when_disabled(
        self,
    ) -> None:
        """Disabled Device Info reads should return before checking client state."""
        controller = BMDCameraController(
            make_discovered(),
            make_profile(device_info_metadata_readable=False),
        )

        await controller.read_device_information_metadata()

        assert controller.manufacturer_info is None
        assert controller.model_info is None

    @pytest.mark.asyncio
    async def test_read_device_information_metadata_does_not_read_when_profile_disables_reads(
        self,
    ) -> None:
        """Disabled Device Info reads should not call ``read_gatt_char``."""
        controller = BMDCameraController(
            make_discovered(),
            make_profile(device_info_metadata_readable=False),
        )
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        controller._client = client

        await controller.read_device_information_metadata()

        assert client.read_calls == []
        assert controller.manufacturer_info is None
        assert controller.model_info is None

    @pytest.mark.asyncio
    async def test_read_device_information_metadata_raises_without_client_when_enabled(
        self,
    ) -> None:
        """Enabled Device Info reads should require an initialized BLE client."""
        controller = BMDCameraController(make_discovered(), make_profile())

        with pytest.raises(RuntimeError, match="Camera is not connected"):
            await controller.read_device_information_metadata()

    @pytest.mark.asyncio
    async def test_read_device_information_metadata_returns_none_when_client_is_disconnected(
        self,
    ) -> None:
        """Enabled Device Info reads should return without values when disconnected."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = False
        controller._client = client

        result = await controller.read_device_information_metadata()

        assert result is None
        assert controller.manufacturer_info is None
        assert controller.model_info is None
        assert client.read_calls == []

    @pytest.mark.asyncio
    async def test_read_device_information_metadata_sets_manufacturer_and_model(
        self,
    ) -> None:
        """Readable manufacturer and model characteristics should populate attributes."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        client.read_values = {
            CHARACTERISTIC_MANUFACTURER_INFO: MANUFACTURER_NAME.encode(),
            CHARACTERISTIC_MODEL_INFO: MODEL_NAME.encode(),
        }
        controller._client = client

        result = await controller.read_device_information_metadata()

        assert result is None
        assert controller.manufacturer_info == MANUFACTURER_NAME
        assert controller.model_info == MODEL_NAME
        assert client.read_calls == [
            CHARACTERISTIC_MANUFACTURER_INFO,
            CHARACTERISTIC_MODEL_INFO,
        ]

    @pytest.mark.asyncio
    async def test_read_device_information_metadata_strips_null_padding(
        self,
    ) -> None:
        """Device Info string values should use the shared UTF-8 decoder."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        client.read_values = {
            CHARACTERISTIC_MANUFACTURER_INFO: b"Blackmagic Design\x00\x00",
            CHARACTERISTIC_MODEL_INFO: b"Pocket Cinema Camera\x00\x00",
        }
        controller._client = client

        await controller.read_device_information_metadata()

        assert controller.manufacturer_info == "Blackmagic Design"
        assert controller.model_info == "Pocket Cinema Camera"

    @pytest.mark.asyncio
    async def test_read_device_information_metadata_attempts_model_after_disconnect(
        self,
    ) -> None:
        """Model Info is still attempted if Manufacturer read disconnects the client."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        controller._client = client

        async def fake_manufacturer_read(characteristic_uuid: str) -> None:
            client.read_calls.append(characteristic_uuid)
            client.is_connected = False
            return None

        controller._read_metadata_characteristic = fake_manufacturer_read

        result = await controller.read_device_information_metadata()

        assert result is None
        assert controller.manufacturer_info is None
        assert controller.model_info is None
        assert client.read_calls == [
            CHARACTERISTIC_MANUFACTURER_INFO,
            CHARACTERISTIC_MODEL_INFO,
        ]

    @pytest.mark.asyncio
    async def test_read_device_information_metadata_propagates_manufacturer_error(
        self,
    ) -> None:
        """Model Info is not attempted when the manufacturer read raises an unhandled error."""
        controller = BMDCameraController(make_discovered(), make_profile())
        client = FakeBleakClient(ADDRESS)
        client.is_connected = True
        client.read_errors[CHARACTERISTIC_MANUFACTURER_INFO] = RuntimeError("unreachable")
        client.read_values[CHARACTERISTIC_MODEL_INFO] = MODEL_NAME.encode()
        controller._client = client

        with pytest.raises(RuntimeError, match="unreachable"):
            await controller.read_device_information_metadata()

        assert controller.manufacturer_info is None
        assert controller.model_info is None
        assert client.read_calls == [CHARACTERISTIC_MANUFACTURER_INFO]


# ── subscribe_incoming ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_incoming_raises_when_client_is_none():
    controller = BMDCameraController(make_discovered(), make_profile())

    with pytest.raises(RuntimeError) as exc_info:
        await controller.subscribe_incoming()

    assert "not connected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscribe_incoming_raises_when_client_not_connected():
    controller = BMDCameraController(make_discovered(), make_profile())

    class FakeDisconnectedClient:
        is_connected = False

    controller._client = FakeDisconnectedClient()

    with pytest.raises(RuntimeError) as exc_info:
        await controller.subscribe_incoming()

    assert "disconnected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscribe_incoming_registers_default_callback():
    controller = BMDCameraController(make_discovered(), make_profile())
    controller._client = FakeConnectedBleakClient(ADDRESS)

    await controller.subscribe_incoming()

    # The registered handler is the generation-guarded wrapper, not the raw callback.
    registered = controller._client.notified[CHARACTERISTIC_INCOMING]
    assert CHARACTERISTIC_INCOMING in controller._client.notified
    assert callable(registered)
    assert registered is not controller._log_incoming


@pytest.mark.asyncio
async def test_subscribe_incoming_registers_custom_callback():
    controller = BMDCameraController(make_discovered(), make_profile())
    controller._client = FakeConnectedBleakClient(ADDRESS)

    def custom_callback(_char, _data):
        pass

    await controller.subscribe_incoming(callback=custom_callback)

    # The registered handler is the generation-guarded wrapper, not the raw callback.
    registered = controller._client.notified[CHARACTERISTIC_INCOMING]
    assert callable(registered)
    assert registered is not custom_callback


@pytest.mark.asyncio
async def test_subscribe_incoming_stores_handler_for_reconnect():
    """``_incoming_callback`` must be set to the resolved handler after subscribe succeeds."""
    controller = BMDCameraController(make_discovered(), make_profile())
    controller._client = FakeConnectedBleakClient(ADDRESS)

    # Default path — no callback arg → resolved to _log_incoming
    await controller.subscribe_incoming()
    assert controller._incoming_callback == controller._log_incoming

    # Custom callback path
    controller._client = FakeConnectedBleakClient(ADDRESS)

    def my_handler(_char, _data):
        pass

    await controller.subscribe_incoming(callback=my_handler)
    assert controller._incoming_callback is my_handler


@pytest.mark.asyncio
async def test_subscribe_incoming_stores_raw_handler_not_guarded_wrapper():
    """``_incoming_callback`` stores the raw callback, not the generation wrapper."""
    controller = BMDCameraController(make_discovered(), make_profile())
    controller._client = FakeConnectedBleakClient(ADDRESS)

    def raw(_char, _data):
        pass

    await controller.subscribe_incoming(callback=raw)

    assert controller._incoming_callback is raw
    assert controller._client.notified[CHARACTERISTIC_INCOMING] is not raw


@pytest.mark.asyncio
async def test_notification_guard_passes_data_when_generation_matches():
    """Guarded handler must invoke the user callback when generation matches."""
    controller = BMDCameraController(make_discovered(), make_profile())
    controller._client = FakeConnectedBleakClient(ADDRESS)
    received: list = []

    def my_callback(_char, data):
        received.append(data)

    await controller.subscribe_incoming(callback=my_callback)
    wrapper = controller._client.notified[CHARACTERISTIC_INCOMING]

    wrapper(None, bytearray(b"\x01\x02"))

    assert len(received) == 1
    assert received[0] == bytearray(b"\x01\x02")


@pytest.mark.asyncio
async def test_notification_guard_drops_data_when_generation_is_stale():
    """Guarded handler must silently drop data when the connection generation has advanced."""
    controller = BMDCameraController(make_discovered(), make_profile())
    controller._client = FakeConnectedBleakClient(ADDRESS)
    received: list = []

    def my_callback(_char, data):
        received.append(data)

    await controller.subscribe_incoming(callback=my_callback)
    wrapper = controller._client.notified[CHARACTERISTIC_INCOMING]

    controller._conn_gen += 1  # Advance generation (simulates a new connect() call)
    wrapper(None, bytearray(b"\x01\x02"))

    assert len(received) == 0


@pytest.mark.asyncio
async def test_guarded_handler_updates_last_rx_time_before_gen_check():
    """Stale notifications must still update ``_last_rx_time`` (WinRT liveness signal)."""
    controller = BMDCameraController(make_discovered(), make_profile())
    controller._client = FakeConnectedBleakClient(ADDRESS)
    callback_invoked = False

    def my_callback(_char, _data):
        nonlocal callback_invoked
        callback_invoked = True

    await controller.subscribe_incoming(callback=my_callback)
    wrapper = controller._client.notified[CHARACTERISTIC_INCOMING]

    # Advance gen so this notification is "stale" and will be dropped.
    controller._conn_gen += 1
    wrapper(None, bytearray(b"\xaa\xbb"))

    assert callback_invoked is False  # dropped by gen guard
    assert controller._last_rx_time is not None  # but timestamp was set


def test_is_receiving_data_returns_false_when_no_rx():
    """``_is_receiving_data`` must return False when no notification has arrived yet."""
    controller = BMDCameraController(make_discovered(), make_profile())

    assert controller._is_receiving_data() is False


def test_is_receiving_data_returns_true_when_rx_is_recent():
    """``_is_receiving_data`` must return True when a notification arrived moments ago."""
    import time

    controller = BMDCameraController(make_discovered(), make_profile())
    controller._last_rx_time = time.monotonic()

    assert controller._is_receiving_data() is True


def test_is_receiving_data_returns_false_when_rx_is_old():
    """``_is_receiving_data`` must return False when the last notification is too old."""
    import time

    controller = BMDCameraController(make_discovered(), make_profile())
    controller._last_rx_time = time.monotonic() - 10.0

    assert controller._is_receiving_data() is False


def test_log_incoming_formats_bytes_as_uppercase_hex(caplog):
    import logging

    controller = BMDCameraController(make_discovered(), make_profile())
    with caplog.at_level(logging.DEBUG, logger="bmd_ble.camera_controller"):
        controller._log_incoming(None, bytearray([0x00, 0x06, 0x0A, 0xFF]))

    assert "00 06 0A FF" in caplog.text


def test_log_incoming_includes_camera_identity_prefix(caplog):
    import logging

    controller = BMDCameraController(make_discovered(), make_profile())
    with caplog.at_level(logging.DEBUG, logger="bmd_ble.camera_controller"):
        controller._log_incoming(None, bytearray([0xAB]))

    assert BLE_NAME in caplog.text
    assert ADDRESS in caplog.text


@pytest.mark.asyncio
async def test_subscribe_incoming_retries_on_transient_bleak_error(monkeypatch):
    """Older firmware (e.g. 6K G2 v7.9) raises BleakError on first CCCD write;
    subscribe_incoming must retry and succeed on the next attempt."""
    controller = BMDCameraController(make_discovered(), make_profile())
    call_count = 0

    class FlakyClient:
        is_connected = True

        def __init__(self, address):
            self.address = address
            self.notified: dict = {}

        async def start_notify(self, uuid, callback):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise BleakError("Could not start notify on 000E: Unreachable")
            self.notified[uuid] = callback

    async def fake_sleep(_):
        pass

    controller._client = FlakyClient(ADDRESS)
    monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)

    await controller.subscribe_incoming()

    assert call_count == 2
    assert CHARACTERISTIC_INCOMING in controller._client.notified


@pytest.mark.asyncio
async def test_subscribe_incoming_raises_after_all_retries_exhausted(monkeypatch):
    controller = BMDCameraController(make_discovered(), make_profile())

    class AlwaysFailClient:
        is_connected = True

        def __init__(self, address):
            self.address = address

        async def start_notify(self, uuid, callback):
            raise BleakError("Could not start notify on 000E: Unreachable")

    async def fake_sleep(_):
        pass

    controller._client = AlwaysFailClient(ADDRESS)
    monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError) as exc_info:
        await controller.subscribe_incoming(retries=2)

    assert "Could not subscribe to INCOMING_CONTROL" in str(exc_info.value)
    assert "after 2 attempts" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscribe_incoming_fast_fails_on_not_connected_error(monkeypatch):
    """A BleakError containing 'not connected' must raise immediately with no retries."""
    controller = BMDCameraController(make_discovered(), make_profile())
    call_count = 0

    class NotConnectedClient:
        is_connected = True

        def __init__(self, address):
            self.address = address

        async def start_notify(self, uuid, callback):
            nonlocal call_count
            call_count += 1
            raise BleakError("Not connected")

    async def fake_sleep(_):
        pass

    controller._client = NotConnectedClient(ADDRESS)
    monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError) as exc_info:
        await controller.subscribe_incoming(retries=3)

    assert call_count == 1
    assert "connection lost mid-subscribe" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscribe_incoming_aborts_retry_when_link_already_dropped(monkeypatch):
    """A link that drops mid-CCCD surfaces on WinRT as OSError ("The operation
    was canceled by the user"), which looks identical to a transient failure.
    The disconnect callback has already fired by then, so ``is_connected`` is
    what distinguishes them — subscribe must abort at once rather than sleep
    ``retry_delay_s`` holding ``_connect_lock`` while ``_reconnect_loop`` waits
    for it. Reproduces the 2026-07-29 POCKET_6K_G2 v8.6 Phase 2 failure."""
    controller = BMDCameraController(make_discovered(), make_profile())
    slept: list[float] = []

    class DropsMidSubscribeClient:
        def __init__(self, address):
            self.address = address
            self.is_connected = True

        async def start_notify(self, uuid, callback):
            # The camera drops the link during the CCCD write; WinRT reports it
            # as OSError and Bleak's disconnect callback flips is_connected.
            self.is_connected = False
            raise OSError("[WinError -2147023673] The operation was canceled by the user.")

    async def fake_sleep(delay):
        slept.append(delay)

    controller._client = DropsMidSubscribeClient(ADDRESS)
    monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError) as exc_info:
        await controller.subscribe_incoming(retries=3, retry_delay_s=10.0)

    assert "connection lost mid-subscribe" in str(exc_info.value)
    assert slept == [], "must not sleep on a retry that cannot succeed"


@pytest.mark.asyncio
async def test_subscribe_incoming_aborts_retry_when_generation_superseded(monkeypatch):
    """If a newer connect() bumped the generation while this subscribe was in
    flight, the subscribe belongs to a dead generation — retrying would attach
    a handler nothing reads. Abort instead of sleeping."""
    controller = BMDCameraController(make_discovered(), make_profile())
    slept: list[float] = []

    class SupersedingClient:
        is_connected = True

        def __init__(self, address):
            self.address = address

        async def start_notify(self, uuid, callback):
            controller._conn_gen += 1
            raise BleakError("Could not start notify on 000E: Unreachable")

    async def fake_sleep(delay):
        slept.append(delay)

    controller._client = SupersedingClient(ADDRESS)
    monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError) as exc_info:
        await controller.subscribe_incoming(retries=3)

    assert "superseded" in str(exc_info.value)
    assert slept == []


@pytest.mark.asyncio
async def test_subscribe_incoming_retries_on_os_error(monkeypatch):
    """``OSError`` from ``start_notify`` should be retried like a transient BleakError."""
    controller = BMDCameraController(make_discovered(), make_profile())
    call_count = 0

    class OSErrorClient:
        is_connected = True

        def __init__(self, address):
            self.address = address
            self.notified: dict = {}

        async def start_notify(self, uuid, callback):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise OSError("WinRT transport error")
            self.notified[uuid] = callback

    async def fake_sleep(_):
        pass

    controller._client = OSErrorClient(ADDRESS)
    monkeypatch.setattr("bmd_ble.camera_controller.asyncio.sleep", fake_sleep)

    await controller.subscribe_incoming()

    assert call_count == 2
    assert CHARACTERISTIC_INCOMING in controller._client.notified
