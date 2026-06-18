"""
Monitor raw INCOMING_CONTROL notifications from a Blackmagic camera.

Run this script, then trigger actions on the camera (start recording,
change a setting, capture a photo). Each incoming notification is logged
as uppercase hex pairs so the output can be compared directly with
Wireshark or nRF Sniffer captures.

Press Ctrl+C to stop monitoring and cleanly disconnect from the camera.

Usage:
    python examples/monitor_incoming.py

Edit MODEL_KEY / FIRMWARE below to target a different camera.
"""

import asyncio
import logging

from bmd_ble import CameraProfile
from bmd_ble.camera_controller import BMDCameraController
from bmd_ble.scanner import scan_for_camera

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v7.9"
# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"


async def main() -> None:
    cam_profile = CameraProfile.for_model(model_key=MODEL_KEY, firmware=FIRMWARE)

    logging.info("Scanning for %s …", cam_profile.ble_name)
    discovered = await scan_for_camera(cam_profile.ble_name)
    logging.info("Found: %s", discovered)

    cam = BMDCameraController(discovered=discovered, profile=cam_profile)
    try:
        await cam.connect()
        logging.info("Connected to %s", cam.discovered.ble_name)
        await cam.subscribe_incoming()
        logging.info(
            "Listening for INCOMING_CONTROL notifications — press Ctrl+C to stop …"
        )
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logging.info("Monitoring stopped.")
    finally:
        await cam.disconnect()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(main())
