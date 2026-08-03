import asyncio
import logging

from bmd_camera import CameraProfile
from bmd_camera.ble.camera_controller import BMDCameraController
from bmd_camera.ble.scanner import scan_for_camera

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v8.6"
# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"


async def main():
    cam_profile = CameraProfile.for_model(model_key=MODEL_KEY, firmware=FIRMWARE)
    discovered_camera = await scan_for_camera(cam_profile.ble_name)
    print(discovered_camera)
    cam = BMDCameraController(discovered=discovered_camera, profile=cam_profile)
    await cam.connect()
    await asyncio.sleep(5)
    if cam._client.is_connected:
        print(f"Connected to {cam.discovered.ble_name}")
        await cam.disconnect()
        print(f"Disconnected from {cam.discovered.ble_name}")
    else:
        print(f"Could not connect to {cam.discovered.ble_name}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
