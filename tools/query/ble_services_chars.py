"""
examples/ble_services_chars.py
==============================

Query BLE GATT services/characteristics from a Blackmagic camera and compare
what the camera exposes against the UUIDs defined in bmd_ble.constants.

Usage:
    python examples/ble_services_chars.py
    python examples/ble_services_chars.py --address AA:BB:CC:DD:EE:FF
    python examples/ble_services_chars.py --name "BMPCC 6K G2"
    python examples/ble_services_chars.py --strict
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from bleak import BleakClient


# Allow running this file directly from either repo root or examples/ without
# installing the package. Walk upward until we find src/bmd_ble.
def find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "src" / "bmd_ble").exists():
            return candidate
    raise RuntimeError("Could not locate repository root containing src/bmd_ble")


REPO_ROOT = find_repo_root(Path(__file__).resolve().parent)
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from bmd_ble import constants  # noqa: E402
from bmd_ble.camera_profile import CameraProfile  # noqa: E402
from bmd_ble.scanner import DiscoveredCamera, scan_for_camera  # noqa: E402

DEFAULT_MODEL_KEY = "POCKET_6K_G2"
DEFAULT_FIRMWARE = "v7.9"


@dataclass(frozen=True)
class ExpectedUuid:
    """UUID value collected from constants.py."""

    const_name: str
    uuid: str
    kind: str  # "service" or "characteristic"
    display_name: str


@dataclass(frozen=True)
class DiscoveredCharacteristic:
    """Characteristic discovered from the connected BLE camera."""

    service_uuid: str
    uuid: str
    description: str
    properties: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveredService:
    """Service discovered from the connected BLE camera."""

    uuid: str
    description: str


def normalize_uuid(uuid: str) -> str:
    """
    Normalize UUIDs so constants and Bleak output can be compared reliably.
    Examples:
        180a -> 0000180a-0000-1000-8000-00805f9b34fb
        2a29 -> 00002a29-0000-1000-8000-00805f9b34fb
    """
    value = str(uuid).strip().lower()
    if len(value) == 4:
        return f"0000{value}-0000-1000-8000-00805f9b34fb"
    return value


def format_properties(properties: Iterable[str]) -> str:
    props = list(properties)
    return ", ".join(props) if props else "none"


def characteristic_names() -> dict[str, str]:
    """Return CHARACTERISTIC_NAMES from constants.py with normalized UUID keys."""
    names = getattr(constants, "CHARACTERISTIC_NAMES", {})
    return {normalize_uuid(uuid): name for uuid, name in names.items()}


def collect_expected_uuids() -> dict[str, ExpectedUuid]:
    """
    Collect service and characteristic UUID constants from bmd_ble.constants.
    Included:
        *_SERVICE_UUID
        CHARACTERISTIC_*
        GAP_CHARACTERISTIC_*

    Excluded:
        BLUETOOTH_BASE_UUID
        CHARACTERISTIC_NAMES
    """
    names = characteristic_names()
    expected: dict[str, ExpectedUuid] = {}

    for const_name, value in inspect.getmembers(constants):
        if not isinstance(value, str):
            continue

        if const_name in {"BLUETOOTH_BASE_UUID"}:
            continue

        is_service = const_name.endswith("SERVICE_UUID")
        is_characteristic = const_name.startswith("CHARACTERISTIC_") or const_name.startswith(
            "GAP_CHARACTERISTIC_"
        )

        if not (is_service or is_characteristic):
            continue

        uuid = normalize_uuid(value)
        kind = "service" if is_service else "characteristic"
        display_name = names.get(uuid, const_name)

        expected[uuid] = ExpectedUuid(
            const_name=const_name,
            uuid=uuid,
            kind=kind,
            display_name=display_name,
        )

    return expected


async def get_services(client: BleakClient):
    """
    Return GATT services in a way that works across Bleak versions.
    """
    services = getattr(client, "services", None)
    if services:
        return services
    get_services = getattr(client, "get_services", None)
    if get_services:
        return await get_services()
    raise RuntimeError("Unable to retrieve GATT services from BleakClient")


async def resolve_camera(args: argparse.Namespace) -> DiscoveredCamera:
    """Resolve the target camera either by explicit address, explicit name, or profile."""

    if args.name:
        return await scan_for_camera(args.name, timeout=args.timeout)

    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    return await scan_for_camera(profile.ble_name, timeout=args.timeout)


def collect_discovered(
    services,
) -> tuple[dict[str, DiscoveredService], dict[str, DiscoveredCharacteristic]]:
    discovered_services: dict[str, DiscoveredService] = {}
    discovered_characteristics: dict[str, DiscoveredCharacteristic] = {}

    for service in services:
        service_uuid = normalize_uuid(service.uuid)
        discovered_services[service_uuid] = DiscoveredService(
            uuid=service_uuid, description=getattr(service, "description", "")
        )

        for char in service.characteristics:
            char_uuid = normalize_uuid(char.uuid)
            discovered_characteristics[char_uuid] = DiscoveredCharacteristic(
                service_uuid=service_uuid,
                uuid=char_uuid,
                description=getattr(char, "description", ""),
                properties=tuple(getattr(char, "properties", [])),
            )

    return discovered_services, discovered_characteristics


def print_discovered_services_and_chars(
    discovered_services: dict[str, DiscoveredService],
    discovered_characteristics: dict[str, DiscoveredCharacteristic],
    expected: dict[str, ExpectedUuid],
) -> None:
    names = characteristic_names()

    print()
    print("GATT Services and Characteristics")
    print("=" * 80)

    chars_by_service: dict[str, list[DiscoveredCharacteristic]] = {}
    for char in discovered_characteristics.values():
        chars_by_service.setdefault(char.service_uuid, []).append(char)

    for service_uuid, service in discovered_services.items():
        expected_service = expected.get(service_uuid)
        service_label = (
            expected_service.display_name if expected_service else "NOT_DEFINED_IN_CONSTANTS"
        )

        print()
        print("SERVICE")
        print(f"  UUID        : {service.uuid}")
        print(f"  Name        : {service_label}")
        print(f"  Description : {service.description}")

        for char in chars_by_service.get(service_uuid, []):
            expected_char = expected.get(char.uuid)
            char_label = (
                expected_char.display_name
                if expected_char
                else names.get(char.uuid, "NOT_DEFINED_IN_CONSTANTS")
            )
            print()
            print("  CHARACTERISTIC")
            print(f"    UUID        : {char.uuid}")
            print(f"    Name        : {char_label}")
            print(f"    Description : {char.description}")
            print(f"    Properties  : {format_properties(char.properties)}")


def print_comparison(
    discovered_services: dict[str, DiscoveredService],
    discovered_characteristics: dict[str, DiscoveredCharacteristic],
    expected: dict[str, ExpectedUuid],
) -> bool:
    """
    Print constants.py vs camera comparison.

    Returns:
        True if there is at least one mismatch, False otherwise.
    """
    expected_services = {uuid: item for uuid, item in expected.items() if item.kind == "service"}
    expected_characteristics = {
        uuid: item for uuid, item in expected.items() if item.kind == "characteristic"
    }

    discovered_service_uuids = set(discovered_services)
    discovered_char_uuids = set(discovered_characteristics)

    expected_service_uuids = set(expected_services)
    expected_char_uuids = set(expected_characteristics)

    missing_services = sorted(expected_service_uuids - discovered_service_uuids)
    extra_services = sorted(discovered_service_uuids - expected_service_uuids)

    missing_characteristics = sorted(expected_char_uuids - discovered_char_uuids)
    extra_characteristics = sorted(discovered_char_uuids - expected_char_uuids)

    print()
    print("Constants.py Comparison")
    print("=" * 80)

    if not missing_services and not missing_characteristics:
        print(
            "OK: All service/characteristic UUIDs defined in constants.py were found on the camera."
        )
    else:
        print("MISMATCH: Some UUIDs defined in constants.py were not found on the camera.")

    if missing_services:
        print()
        print("Missing services defined in constants.py:")
        for uuid in missing_services:
            item = expected_services[uuid]
            print(f"  - {item.const_name}: {uuid}")

    if missing_characteristics:
        print()
        print("Missing characteristics defined in constants.py:")
        for uuid in missing_characteristics:
            item = expected_characteristics[uuid]
            print(f"  - {item.const_name}: {uuid} ({item.display_name})")

    if extra_services:
        print()
        print("Extra services found on camera but not defined in constants.py:")
        for uuid in extra_services:
            service = discovered_services[uuid]
            print(f"  - {uuid} ({service.description})")

    if extra_characteristics:
        print()
        print("Extra characteristics found on camera but not defined in constants.py:")
        for uuid in extra_characteristics:
            char = discovered_characteristics[uuid]
            print(
                f"  - {uuid} | service={char.service_uuid} | "
                f"properties={format_properties(char.properties)} | description={char.description}"
            )

    print()
    print("Summary")
    print("-" * 80)
    print(f"Expected services        : {len(expected_services)}")
    print(f"Discovered services      : {len(discovered_services)}")
    print(f"Missing services         : {len(missing_services)}")
    print(f"Extra services           : {len(extra_services)}")
    print(f"Expected characteristics : {len(expected_characteristics)}")
    print(f"Discovered characteristics: {len(discovered_characteristics)}")
    print(f"Missing characteristics  : {len(missing_characteristics)}")
    print(f"Extra characteristics    : {len(extra_characteristics)}")

    return bool(
        missing_services or missing_characteristics or extra_services or extra_characteristics
    )


async def run(args: argparse.Namespace) -> int:
    expected = collect_expected_uuids()
    discovered_camera = await resolve_camera(args)

    print("Target camera")
    print("=" * 80)
    print(f"Name    : {discovered_camera.ble_name}")
    print(f"Address : {discovered_camera.address}")
    print(f"RSSI    : {discovered_camera.rssi}")

    async with BleakClient(discovered_camera.address) as client:
        services = await get_services(client)
        discovered_services, discovered_characteristics = collect_discovered(services)

        print_discovered_services_and_chars(
            discovered_services=discovered_services,
            discovered_characteristics=discovered_characteristics,
            expected=expected,
        )

        has_mismatch = print_comparison(
            discovered_services=discovered_services,
            discovered_characteristics=discovered_characteristics,
            expected=expected,
        )

    if args.strict and has_mismatch:
        return 1

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Query BLE GATT services/characteristics and compare them against "
            "UUID constants defined in bmd_ble.constants."
        )
    )

    parser.add_argument(
        "--model-key",
        default=DEFAULT_MODEL_KEY,
        help=f"Camera model key used to load CameraProfile. Default: {DEFAULT_MODEL_KEY}",
    )
    parser.add_argument(
        "--firmware",
        default=DEFAULT_FIRMWARE,
        help=f"Camera firmware used to load CameraProfile. Default: {DEFAULT_FIRMWARE}",
    )
    parser.add_argument(
        "--name",
        help="BLE advertisement name to scan for. Overrides profile ble_name if provided.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="BLE scan timeout in seconds. Default: 15.0",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 if any constants.py mismatch is found.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    raise SystemExit(asyncio.run(run(parse_args())))
