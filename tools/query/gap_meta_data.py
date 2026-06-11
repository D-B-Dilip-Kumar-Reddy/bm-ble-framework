import argparse
import asyncio
import logging

from bmd_ble.camera_controller import BMDCameraController
from bmd_ble.camera_profile import CameraProfile
from bmd_ble.scanner import scan_for_camera

# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"


async def run(args: argparse.Namespace):
    model_key = args.model_key
    firmware = args.firmware
    cam_profile = CameraProfile.for_model(model_key=model_key, firmware=firmware)
    discovered_camera = await scan_for_camera(cam_profile.ble_name)
    print(discovered_camera)
    cam = BMDCameraController(discovered_camera)
    await cam.connect()
    print(f"Connected to {cam.discovered.ble_name}")
    # await asyncio.sleep(5)
    await cam.read_gap_identity_metadata()
    print(f"GAP device name: {cam.gap_device_name}")
    print(f"GAP appearance: {cam.gap_appearance}")
    await cam.disconnect()
    print(f"Disconnected from {cam.discovered.ble_name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=("Get Generic Access Profile data via BLE."))
    parser.add_argument(
        "--model-key",
        help="Camera model key used to load CameraProfile.",
    )
    parser.add_argument(
        "--firmware",
        help="Camera firmware used to load CameraProfile.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="BLE scan timeout in seconds. Default: 15.0",
    )

    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    raise SystemExit(asyncio.run(run(parse_args())))
