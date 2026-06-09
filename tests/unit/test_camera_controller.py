import asyncio

import pytest
from bleak import BleakError

from bmd_ble import CHARACTERISTIC_INCOMING, CHARACTERISTIC_CAM_STATUS, \
    CHARACTERISTIC_TIMECODE
from bmd_ble.camera_controller import BMDCameraController
from bmd_ble.scanner import DiscoveredCamera


class FakeBleakClient:
    def __init__(self, address):
        self.address = address
        self.connect_called = False

    async def connect(self):
        self.connect_called = True


def test_controller_initializes_with_discovered_camera():
    discovered = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:01",
        ble_name="BMPCC 6K G2",
        rssi=-45,
    )

    controller = BMDCameraController(discovered)

    assert controller.discovered == discovered
    assert controller._client is None


@pytest.mark.asyncio
async def test_connect_uses_existing_address_without_scanning(monkeypatch):
    discovered = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:01",
        ble_name="BMPCC 6K G2",
        rssi=-45,
    )

    controller = BMDCameraController(discovered)

    async def fake_scan_for_camera(ble_name):
        raise AssertionError("scan_for_camera should not be called")

    monkeypatch.setattr(
        "bmd_ble.camera_controller.scan_for_camera",
        fake_scan_for_camera,
    )
    monkeypatch.setattr(
        "bmd_ble.camera_controller.BleakClient",
        FakeBleakClient,
    )

    await controller.connect()

    assert controller._client is not None
    assert controller._client.address == "AA:BB:CC:DD:EE:01"
    assert controller._client.connect_called is True
    assert controller.discovered.address == "AA:BB:CC:DD:EE:01"


@pytest.mark.asyncio
async def test_connect_scans_when_address_is_missing(monkeypatch):
    discovered = DiscoveredCamera(
        address="",
        ble_name="BMPCC 6K G2",
        rssi=None,
    )

    scanned_camera = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:02",
        ble_name="BMPCC 6K G2",
        rssi=-50,
    )

    controller = BMDCameraController(discovered)

    async def fake_scan_for_camera(ble_name):
        assert ble_name == "BMPCC 6K G2"
        return scanned_camera

    monkeypatch.setattr(
        "bmd_ble.camera_controller.scan_for_camera",
        fake_scan_for_camera,
    )
    monkeypatch.setattr(
        "bmd_ble.camera_controller.BleakClient",
        FakeBleakClient,
    )

    await controller.connect()

    assert controller.discovered.address == "AA:BB:CC:DD:EE:02"
    assert controller.discovered.ble_name == "BMPCC 6K G2"
    assert controller.discovered.rssi == -50
    assert controller._client.address == "AA:BB:CC:DD:EE:02"
    assert controller._client.connect_called is True


@pytest.mark.asyncio
async def test_connect_uses_wait_for_with_configured_timeout(monkeypatch):
    discovered = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:03",
        ble_name="BMPCC 6K G2",
        rssi=-60,
    )

    controller = BMDCameraController(discovered)

    captured_timeout = None

    async def fake_wait_for(coro, timeout):
        nonlocal captured_timeout
        captured_timeout = timeout
        return await coro

    monkeypatch.setattr(
        "bmd_ble.camera_controller.BleakClient",
        FakeBleakClient,
    )
    monkeypatch.setattr(
        "bmd_ble.camera_controller.asyncio.wait_for",
        fake_wait_for,
    )

    await controller.connect()

    assert captured_timeout == 10.0
    assert controller._client.connect_called is True


@pytest.mark.asyncio
async def test_connect_raises_runtime_error_on_timeout(monkeypatch):
    discovered = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:04",
        ble_name="BMPCC 6K G2",
        rssi=-65,
    )

    controller = BMDCameraController(discovered)

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(
        "bmd_ble.camera_controller.BleakClient",
        FakeBleakClient,
    )
    monkeypatch.setattr(
        "bmd_ble.camera_controller.asyncio.wait_for",
        fake_wait_for,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await controller.connect()

    assert "[BMPCC 6K G2] Connect failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_connect_raises_runtime_error_on_bleak_error(monkeypatch):
    discovered = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:05",
        ble_name="BMPCC 6K G2",
        rssi=-70,
    )

    controller = BMDCameraController(discovered)

    class FailingBleakClient:
        def __init__(self, address):
            self.address = address

        async def connect(self):
            raise BleakError("BLE connection failed")

    monkeypatch.setattr(
        "bmd_ble.camera_controller.BleakClient",
        FailingBleakClient,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await controller.connect()

    assert "[BMPCC 6K G2] Connect failed" in str(exc_info.value)
    assert "BLE connection failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_connect_scanning_failure_is_propagated(monkeypatch):
    discovered = DiscoveredCamera(
        address="",
        ble_name="BMPCC 6K G2",
        rssi=None,
    )

    controller = BMDCameraController(discovered)

    async def fake_scan_for_camera(ble_name):
        raise RuntimeError("No camera found")

    monkeypatch.setattr(
        "bmd_ble.camera_controller.scan_for_camera",
        fake_scan_for_camera,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await controller.connect()

    assert "No camera found" in str(exc_info.value)
    assert controller._client is None


@pytest.mark.asyncio
async def test_disconnect_does_nothing_when_client_is_none():
    discovered = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:01",
        ble_name="BMPCC 6K G2",
        rssi=-45,
    )

    controller = BMDCameraController(discovered)

    await controller.disconnect()

    assert controller._client is None


@pytest.mark.asyncio
async def test_disconnect_does_not_disconnect_when_client_not_connected():
    discovered = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:02",
        ble_name="BMPCC 6K G2",
        rssi=-50,
    )

    controller = BMDCameraController(discovered)

    class FakeDisconnectedClient:
        is_connected = False

        def __init__(self):
            self.disconnect_called = False
            self.stop_notify_called = False

        async def stop_notify(self, char):
            self.stop_notify_called = True

        async def disconnect(self):
            self.disconnect_called = True

    fake_client = FakeDisconnectedClient()
    controller._client = fake_client

    await controller.disconnect()

    assert fake_client.stop_notify_called is False
    assert fake_client.disconnect_called is False


@pytest.mark.asyncio
async def test_disconnect_stops_notifications_and_disconnects_when_connected():
    discovered = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:03",
        ble_name="BMPCC 6K G2",
        rssi=-55,
    )

    controller = BMDCameraController(discovered)

    class FakeConnectedClient:
        is_connected = True

        def __init__(self):
            self.stopped_notifications = []
            self.disconnect_called = False

        async def stop_notify(self, char):
            self.stopped_notifications.append(char)

        async def disconnect(self):
            self.disconnect_called = True

    fake_client = FakeConnectedClient()
    controller._client = fake_client

    await controller.disconnect()

    assert fake_client.stopped_notifications == [
        CHARACTERISTIC_INCOMING,
        CHARACTERISTIC_CAM_STATUS,
        CHARACTERISTIC_TIMECODE,
    ]
    assert fake_client.disconnect_called is True


@pytest.mark.asyncio
async def test_disconnect_ignores_bleak_error_from_stop_notify():
    discovered = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:04",
        ble_name="BMPCC 6K G2",
        rssi=-60,
    )

    controller = BMDCameraController(discovered)

    class StopNotifyFailingClient:
        is_connected = True

        def __init__(self):
            self.stop_notify_calls = []
            self.disconnect_called = False

        async def stop_notify(self, char):
            self.stop_notify_calls.append(char)
            raise BleakError("notification already stopped")

        async def disconnect(self):
            self.disconnect_called = True

    fake_client = StopNotifyFailingClient()
    controller._client = fake_client

    await controller.disconnect()

    assert fake_client.stop_notify_calls == [
        CHARACTERISTIC_INCOMING,
        CHARACTERISTIC_CAM_STATUS,
        CHARACTERISTIC_TIMECODE,
    ]
    assert fake_client.disconnect_called is True


@pytest.mark.asyncio
async def test_disconnect_propagates_disconnect_error():
    discovered = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:05",
        ble_name="BMPCC 6K G2",
        rssi=-65,
    )

    controller = BMDCameraController(discovered)

    class DisconnectFailingClient:
        is_connected = True

        async def stop_notify(self, char):
            return None

        async def disconnect(self):
            raise BleakError("disconnect failed")

    controller._client = DisconnectFailingClient()

    with pytest.raises(BleakError) as exc_info:
        await controller.disconnect()

    assert "disconnect failed" in str(exc_info.value)