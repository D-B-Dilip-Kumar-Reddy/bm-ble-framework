"""
tools/sniffers/sniffer_recording.py
====================================
Sniffer for the recording category (record start / record stop).

Connects to the camera, then runs two interactive capture windows:
  1. record_start — trigger recording to start on the physical camera
  2. record_stop  — trigger recording to stop on the physical camera

For each window, prints every (characteristic, category, parameter) triple
observed on INCOMING_CONTROL / CAMERA_STATUS, and saves the full decoded
capture to tools/sniffers/captures/ for later use populating
payloads/models/<MODEL_KEY>_<FIRMWARE>.json — see docs/recording.md,
"Remaining work".

Usage:
    python tools/sniffers/sniffer_recording.py
    python tools/sniffers/sniffer_recording.py --model-key POCKET_6K_G2 --firmware v7.9
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from capture import print_window_summary, run_capture_windows, save_capture

from bmd_ble.camera_controller import BMDCameraController
from bmd_ble.camera_profile import CameraProfile
from bmd_ble.scanner import scan_for_camera

DEFAULT_MODEL_KEY = "POCKET_6K_G2"
DEFAULT_FIRMWARE = "v7.9"
ACTION_LABELS = ["record_start", "record_stop"]


async def run(args: argparse.Namespace) -> int:
    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    discovered = await scan_for_camera(profile.ble_name, timeout=args.timeout)
    cam = BMDCameraController(discovered=discovered, profile=profile)

    await cam.connect()
    try:
        session = await run_capture_windows(cam, ACTION_LABELS)
        for window in session.windows:
            print_window_summary(window)
        saved_path = save_capture(args.model_key, args.firmware, session)
        print(f"\nCapture saved to: {saved_path}")
    finally:
        await cam.disconnect()

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sniff record start / record stop BLE notifications from a camera."
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
