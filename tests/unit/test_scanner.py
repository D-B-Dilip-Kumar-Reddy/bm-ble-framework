import pytest

from bmd_camera.ble.scanner import DiscoveredCamera, scan_for_camera


class FakeDevice:
    def __init__(self, name, address):
        self.name = name
        self.address = address


class FakeAdvertisementData:
    def __init__(self, rssi):
        self.rssi = rssi


@pytest.mark.asyncio
async def test_scan_for_camera_returns_discovered_camera(monkeypatch):
    devices = [
        (
            FakeDevice("BMPCC 6K G2", "AA:BB:CC:DD:EE:01"),
            FakeAdvertisementData(-45),
        )
    ]

    class FakeBleakScanner:
        def __init__(self, detection_callback):
            self.detection_callback = detection_callback

        async def __aenter__(self):
            for device, adv in devices:
                self.detection_callback(device, adv)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_sleep(timeout):
        return None

    monkeypatch.setattr("bmd_camera.ble.scanner.BleakScanner", FakeBleakScanner)
    monkeypatch.setattr("bmd_camera.ble.scanner.asyncio.sleep", fake_sleep)

    result = await scan_for_camera("BMPCC 6K G2", timeout=0.01)

    assert isinstance(result, DiscoveredCamera)
    assert result.address == "AA:BB:CC:DD:EE:01"
    assert result.ble_name == "BMPCC 6K G2"
    assert result.rssi == -45


@pytest.mark.asyncio
async def test_scan_for_camera_matches_case_insensitive_name(monkeypatch):
    devices = [
        (
            FakeDevice("Blackmagic Pocket 6K G2", "AA:BB:CC:DD:EE:02"),
            FakeAdvertisementData(-50),
        )
    ]

    class FakeBleakScanner:
        def __init__(self, detection_callback):
            self.detection_callback = detection_callback

        async def __aenter__(self):
            for device, adv in devices:
                self.detection_callback(device, adv)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_sleep(timeout):
        return None

    monkeypatch.setattr("bmd_camera.ble.scanner.BleakScanner", FakeBleakScanner)
    monkeypatch.setattr("bmd_camera.ble.scanner.asyncio.sleep", fake_sleep)

    result = await scan_for_camera("pocket 6k", timeout=0.01)

    assert result.address == "AA:BB:CC:DD:EE:02"
    assert result.ble_name == "Blackmagic Pocket 6K G2"
    assert result.rssi == -50


@pytest.mark.asyncio
async def test_scan_for_camera_strips_query_whitespace(monkeypatch):
    devices = [
        (
            FakeDevice("BMPCC 6K G2", "AA:BB:CC:DD:EE:03"),
            FakeAdvertisementData(-55),
        )
    ]

    class FakeBleakScanner:
        def __init__(self, detection_callback):
            self.detection_callback = detection_callback

        async def __aenter__(self):
            for device, adv in devices:
                self.detection_callback(device, adv)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_sleep(timeout):
        return None

    monkeypatch.setattr("bmd_camera.ble.scanner.BleakScanner", FakeBleakScanner)
    monkeypatch.setattr("bmd_camera.ble.scanner.asyncio.sleep", fake_sleep)

    result = await scan_for_camera("   bmpcc 6k   ", timeout=0.01)

    assert result.address == "AA:BB:CC:DD:EE:03"
    assert result.ble_name == "BMPCC 6K G2"
    assert result.rssi == -55


@pytest.mark.asyncio
async def test_scan_for_camera_ignores_non_matching_devices(monkeypatch):
    devices = [
        (
            FakeDevice("Random Speaker", "AA:BB:CC:DD:EE:04"),
            FakeAdvertisementData(-30),
        ),
        (
            FakeDevice("BMPCC 6K G2", "AA:BB:CC:DD:EE:05"),
            FakeAdvertisementData(-70),
        ),
    ]

    class FakeBleakScanner:
        def __init__(self, detection_callback):
            self.detection_callback = detection_callback

        async def __aenter__(self):
            for device, adv in devices:
                self.detection_callback(device, adv)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_sleep(timeout):
        return None

    monkeypatch.setattr("bmd_camera.ble.scanner.BleakScanner", FakeBleakScanner)
    monkeypatch.setattr("bmd_camera.ble.scanner.asyncio.sleep", fake_sleep)

    result = await scan_for_camera("BMPCC", timeout=0.01)

    assert result.address == "AA:BB:CC:DD:EE:05"
    assert result.ble_name == "BMPCC 6K G2"
    assert result.rssi == -70


@pytest.mark.asyncio
async def test_scan_for_camera_selects_strongest_rssi(monkeypatch):
    devices = [
        (
            FakeDevice("BMPCC 6K G2", "AA:BB:CC:DD:EE:06"),
            FakeAdvertisementData(-80),
        ),
        (
            FakeDevice("BMPCC 6K G2", "AA:BB:CC:DD:EE:07"),
            FakeAdvertisementData(-40),
        ),
        (
            FakeDevice("BMPCC 6K G2", "AA:BB:CC:DD:EE:08"),
            FakeAdvertisementData(-60),
        ),
    ]

    class FakeBleakScanner:
        def __init__(self, detection_callback):
            self.detection_callback = detection_callback

        async def __aenter__(self):
            for device, adv in devices:
                self.detection_callback(device, adv)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_sleep(timeout):
        return None

    monkeypatch.setattr("bmd_camera.ble.scanner.BleakScanner", FakeBleakScanner)
    monkeypatch.setattr("bmd_camera.ble.scanner.asyncio.sleep", fake_sleep)

    result = await scan_for_camera("BMPCC 6K G2", timeout=0.01)

    assert result.address == "AA:BB:CC:DD:EE:07"
    assert result.rssi == -40


@pytest.mark.asyncio
async def test_scan_for_camera_handles_none_rssi_as_weak_signal(monkeypatch):
    devices = [
        (
            FakeDevice("BMPCC 6K G2", "AA:BB:CC:DD:EE:09"),
            FakeAdvertisementData(None),
        ),
        (
            FakeDevice("BMPCC 6K G2", "AA:BB:CC:DD:EE:10"),
            FakeAdvertisementData(-65),
        ),
    ]

    class FakeBleakScanner:
        def __init__(self, detection_callback):
            self.detection_callback = detection_callback

        async def __aenter__(self):
            for device, adv in devices:
                self.detection_callback(device, adv)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_sleep(timeout):
        return None

    monkeypatch.setattr("bmd_camera.ble.scanner.BleakScanner", FakeBleakScanner)
    monkeypatch.setattr("bmd_camera.ble.scanner.asyncio.sleep", fake_sleep)

    result = await scan_for_camera("BMPCC 6K G2", timeout=0.01)

    assert result.address == "AA:BB:CC:DD:EE:10"
    assert result.rssi == -65


@pytest.mark.asyncio
async def test_scan_for_camera_raises_runtime_error_when_no_camera_found(monkeypatch):
    devices = [
        (
            FakeDevice("Random BLE Device", "AA:BB:CC:DD:EE:11"),
            FakeAdvertisementData(-45),
        )
    ]

    class FakeBleakScanner:
        def __init__(self, detection_callback):
            self.detection_callback = detection_callback

        async def __aenter__(self):
            for device, adv in devices:
                self.detection_callback(device, adv)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_sleep(timeout):
        return None

    monkeypatch.setattr("bmd_camera.ble.scanner.BleakScanner", FakeBleakScanner)
    monkeypatch.setattr("bmd_camera.ble.scanner.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError) as exc_info:
        await scan_for_camera("BMPCC 6K G2", timeout=0.01)

    assert "No camera matching 'BMPCC 6K G2' found" in str(exc_info.value)


def test_discovered_camera_defaults_rssi_to_none():
    camera = DiscoveredCamera(
        address="AA:BB:CC:DD:EE:12",
        ble_name="BMPCC 6K G2",
    )

    assert camera.address == "AA:BB:CC:DD:EE:12"
    assert camera.ble_name == "BMPCC 6K G2"
    assert camera.rssi is None
