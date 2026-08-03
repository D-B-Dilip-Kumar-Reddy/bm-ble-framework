"""
tools/control/send_record_command.py
=====================================
Actively sends the record start / record stop command to a real camera and
captures the response. Unlike tools/sniffers/sniffer_recording.py (which
only listens, waiting for the operator to trigger the action out-of-band),
this tool triggers the action itself via
BMDCameraController.write_outgoing_control — it WILL start and stop
recording on the connected camera.

Command bytes are built from the profile's `commands.recording` block
(never hardcoded) — see payloads/models/POCKET_6K_G2_v7.9.json and
docs/ble/recording.md.

Usage:
    python tools/control/send_record_command.py
    python tools/control/send_record_command.py --model-key POCKET_6K_G2 --firmware v7.9
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from capture import configure_console_logging, run_send_and_capture, save_capture  # noqa: E402

from bmd_camera.ble.camera_controller import BMDCameraController  # noqa: E402
from bmd_camera.ble.protocol.categories.recording import (  # noqa: E402
    encode_record_start,
    encode_record_stop,
)
from bmd_camera.ble.scanner import scan_for_camera  # noqa: E402
from bmd_camera.camera_profile import CameraProfile  # noqa: E402

DEFAULT_MODEL_KEY = "POCKET_6K_G2"
DEFAULT_FIRMWARE = "v8.6"


async def run(args: argparse.Namespace) -> int:
    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    spec = profile.require_command("recording", ("start", "stop"))

    start_bytes = encode_record_start(
        category=spec.category,
        parameter=spec.parameter,
        data_type=spec.data_type,
        value=spec.values["start"],
        reserved=spec.reserved,
    )
    stop_bytes = encode_record_stop(
        category=spec.category,
        parameter=spec.parameter,
        data_type=spec.data_type,
        value=spec.values["stop"],
        reserved=spec.reserved,
    )

    discovered = await scan_for_camera(profile.ble_name, timeout=args.timeout)
    cam = BMDCameraController(discovered=discovered, profile=profile)

    await cam.connect()
    try:
        print("Sending record_start...")
        start_session = await run_send_and_capture(
            cam, [("record_start", start_bytes)], listen_seconds=args.listen_seconds
        )

        print(f"\nRecording for {args.hold_seconds}s before sending record_stop...")
        await asyncio.sleep(args.hold_seconds)

        print("Sending record_stop...")
        stop_session = await run_send_and_capture(
            cam, [("record_stop", stop_bytes)], listen_seconds=args.listen_seconds
        )

        combined = start_session
        combined.windows.extend(stop_session.windows)
        saved_path = save_capture(args.model_key, args.firmware, combined)
        print(f"\nCapture saved to: {saved_path}")
    finally:
        await cam.disconnect()

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Actively send the record start/stop command to a real camera and "
            "capture the response. This WILL start and stop recording."
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
        "--timeout",
        type=float,
        default=15.0,
        help="BLE scan timeout in seconds. Default: 15.0",
    )
    parser.add_argument(
        "--listen-seconds",
        type=float,
        default=3.0,
        help="Seconds to listen for a response after each command. Default: 3.0",
    )
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=3.0,
        help="Seconds to wait between sending start and stop. Default: 3.0",
    )

    return parser.parse_args()


if __name__ == "__main__":
    configure_console_logging()
    raise SystemExit(asyncio.run(run(parse_args())))
