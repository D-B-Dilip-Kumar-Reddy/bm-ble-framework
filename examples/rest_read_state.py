"""
Read a camera's current state entirely over REST — no BLE involved.

Demonstrates RestCameraSession's read-only surface (Phase 3): current
format, the camera's own supported-formats capability matrix (if this
camera/firmware's rest/ profile confirms the endpoint), storage state,
clips, timecode, and the notification-driven `is_recording` flag.

Edit HOST / MODEL_KEY / FIRMWARE below to target a different camera. HOST
is the camera's address over USB — see docs/rest/transport.md for how to
find it (prefer the mDNS `.local` name; fall back to the numeric IP from
`Resolve-DnsName` if that name doesn't resolve for your Python install).

Usage:
    python examples/rest_read_state.py
"""

import asyncio
import logging

from bmd_camera import RestCameraSession
from bmd_camera.exceptions import BMDUnsupportedError

HOST = "pocket-cinema-camera-6k-pro.local"
MODEL_KEY = "POCKET_6K_PRO"
FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-g2.local"
# MODEL_KEY = "POCKET_6K_G2"
# FIRMWARE = "v8.6"


async def main() -> None:
    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        fmt = await session.get_format()
        print(f"Format: {fmt}")

        try:
            formats = await session.supported_formats()
            print(f"Supported formats: {len(formats)} combinations")
        except BMDUnsupportedError as exc:
            print(f"Supported formats: {exc}")

        storage = await session.storage_state()
        if storage.active_device is not None:
            print(
                f"Active storage: {storage.active_device.device_name} "
                f"({storage.active_device.remaining_record_time}s remaining, "
                f"{storage.active_device.clip_count} clips)"
            )
        else:
            print("Active storage: none reporting active")

        clips = await session.clips()
        print(f"Clips: {len(clips)}")

        tc = await session.timecode()
        print(f"Timecode: {tc.hours:02d}:{tc.minutes:02d}:{tc.seconds:02d}:{tc.frames:02d}")

        print(f"Recording: {session.is_recording}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
