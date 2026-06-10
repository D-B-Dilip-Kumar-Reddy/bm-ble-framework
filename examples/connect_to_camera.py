import asyncio
import logging

from bmd_ble import CameraProfile
from bmd_ble.camera_controller import BMDCameraController
from bmd_ble.scanner import scan_for_camera

MODEL_KEY = "POCKET_6K_PRO"
FIRMWARE = "v8.6"

async def main():
    cam_profile = CameraProfile.for_model(model_key=MODEL_KEY, firmware=FIRMWARE)
    discovered_camera = await scan_for_camera(cam_profile.ble_name)
    print(discovered_camera)
    cam = BMDCameraController(discovered_camera)
    await cam.connect()
    print("Connected to {}".format(cam.discovered.ble_name))
    await asyncio.sleep(5)
    await cam.disconnect()
    print("Disconnected from {}".format(cam.discovered.ble_name))

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    asyncio.run(main())