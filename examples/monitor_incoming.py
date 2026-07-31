"""
Monitor raw INCOMING_CONTROL, TIMECODE, and CAMERA_STATUS notifications
from a Blackmagic camera.

Run this script, then trigger actions on the camera (start recording,
change a setting, capture a photo). Each incoming notification is logged
as uppercase hex pairs so the output can be compared directly with
Wireshark or nRF Sniffer captures.

All three characteristics are subscribed automatically on connect.
Notifications are also written to a timestamped log file under
logs/<model_key>_<firmware>/ in the current working directory.

Press Ctrl+C to stop monitoring and cleanly disconnect from the camera.
Set MONITOR_DURATION_S to a positive integer to auto-stop after that many seconds.

Usage:
    python examples/monitor_incoming.py

Edit MODEL_KEY / FIRMWARE / MONITOR_DURATION_S below to target a different camera
or change the capture duration.
"""

import asyncio
import logging
import time

from bmd_ble import CameraProfile
from bmd_ble.camera_controller import BMDCameraController
from bmd_ble.scanner import scan_for_camera

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v8.6"
# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"

# Set to a positive integer to auto-stop after that many seconds.
# None = run until Ctrl+C.
MONITOR_DURATION_S: int | None = None


async def main() -> None:
    cam_profile = CameraProfile.for_model(model_key=MODEL_KEY, firmware=FIRMWARE)

    logging.info("Scanning for %s …", cam_profile.ble_name)
    discovered = await scan_for_camera(cam_profile.ble_name)
    logging.info("Found: %s", discovered)

    cam = BMDCameraController(discovered=discovered, profile=cam_profile)
    try:
        await cam.connect()  # subscribes INCOMING_CONTROL, TIMECODE, CAMERA_STATUS automatically
        if MONITOR_DURATION_S:
            logging.info("Monitoring for %d s — press Ctrl+C to stop early …", MONITOR_DURATION_S)
        else:
            logging.info("Monitoring — press Ctrl+C to stop …")
        deadline = (time.monotonic() + MONITOR_DURATION_S) if MONITOR_DURATION_S else None
        while True:
            await asyncio.sleep(0.5)
            if deadline and time.monotonic() >= deadline:
                break
    except asyncio.CancelledError:
        logging.info("Monitoring stopped by Ctrl+C.")
    finally:
        await cam.disconnect()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(main())
