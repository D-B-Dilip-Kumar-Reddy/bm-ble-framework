import asyncio
import logging

from bmd_ble.camera_profile import CameraProfile
from bmd_ble.scanner import scan_for_camera

MODEL_KEY = "URSA_MINI_PRO_12K"
FIRMWARE = "v8.1"


async def main():
    cam_profile = CameraProfile.for_model(model_key=MODEL_KEY, firmware=FIRMWARE)
    discovered_camera = await scan_for_camera(cam_profile.ble_name)
    print(discovered_camera)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
