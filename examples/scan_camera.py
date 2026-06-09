import asyncio
import logging

from bmd_ble.scanner import scan_for_camera
from bmd_ble.camera_profile import CameraProfile

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v7.9"

async def main():
    cam_profile = CameraProfile.for_model(model_key=MODEL_KEY, firmware=FIRMWARE)
    result = await scan_for_camera(cam_profile.ble_name)
    print(result)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    asyncio.run(main())