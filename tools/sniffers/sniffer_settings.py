"""
tools/sniffers/sniffer_settings.py
===================================
Passive sniffer for the settings families — codec, quality variant,
resolution, and FPS changes (see docs/settings.md).

Connects to the camera, then runs one interactive capture window per action
label. The operator triggers each change on the physical camera (menu /
touchscreen) between two Enter presses; the tool only listens. For each
window it prints every (characteristic, category, parameter) triple observed
on INCOMING_CONTROL / CAMERA_STATUS and saves the full decoded capture to
tools/captures/.

Two ways to use it:

1. Verify the CANDIDATE POCKET_6K_G2 v7.9 families (default labels): change
   codec BRAW <-> ProRes, a quality variant, a resolution, and the frame
   rate on the body, then check the captured packets against the byte
   layouts in docs/settings.md (codec_quality 0x0A/0x00, video_format
   0x01/0x00, recording_format 0x01/0x09).

2. Reverse-engineer another model's value tables with custom labels — one
   window per concrete setting so each capture is unambiguously attributable,
   e.g. mapping every resolution's dimension_enum:

       python tools/sniffers/sniffer_settings.py \\
           --model-key POCKET_6K_PRO --firmware v8.6 \\
           --actions res_HD,res_UHD,res_4K_DCI,res_6K,codec_prores,codec_braw

   The saved capture then seeds tools/control/discover_command.py
   (--from-capture) or is transcribed directly into the profile's
   commands/codecs/resolutions/fps_modes sections — see docs/settings.md's
   runbook.

This tool only listens — it never writes to OUTGOING_CONTROL. For a tool
that actively sends the settings commands already in a profile and captures
the response, see tools/control/send_settings_command.py.

Usage:
    python tools/sniffers/sniffer_settings.py
    python tools/sniffers/sniffer_settings.py --model-key POCKET_6K_PRO --firmware v8.6
    python tools/sniffers/sniffer_settings.py --actions res_HD,res_UHD,fps_50
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from capture import (  # noqa: E402
    configure_console_logging,
    print_window_summary,
    run_capture_windows,
    save_capture,
)

from bmd_ble.camera_controller import BMDCameraController  # noqa: E402
from bmd_ble.camera_profile import CameraProfile  # noqa: E402
from bmd_ble.scanner import scan_for_camera  # noqa: E402

DEFAULT_MODEL_KEY = "POCKET_6K_G2"
DEFAULT_FIRMWARE = "v7.9"
# One window per settings change the docs/settings.md families cover. The
# two codec windows come first deliberately: a BRAW -> ProRes -> BRAW round
# trip leaves the camera in its starting family before the smaller changes.
DEFAULT_ACTION_LABELS = [
    "codec_to_prores",
    "codec_to_braw",
    "quality_variant_change",
    "resolution_change",
    "fps_change",
]


async def run(args: argparse.Namespace) -> int:
    labels = [label.strip() for label in args.actions.split(",") if label.strip()]
    if not labels:
        raise SystemExit("--actions must name at least one capture window label.")

    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    discovered = await scan_for_camera(profile.ble_name, timeout=args.timeout)
    cam = BMDCameraController(discovered=discovered, profile=profile)

    await cam.connect()
    try:
        session = await run_capture_windows(cam, labels)
        for window in session.windows:
            print_window_summary(window)
        saved_path = save_capture(args.model_key, args.firmware, session)
        print(f"\nCapture saved to: {saved_path}")
    finally:
        await cam.disconnect()

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sniff codec / quality / resolution / FPS settings-change BLE "
            "notifications from a camera while the operator changes each "
            "setting on the body."
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
        "--actions",
        default=",".join(DEFAULT_ACTION_LABELS),
        help=(
            "Comma-separated capture window labels, one window per operator-"
            "triggered change. Use one label per concrete setting when "
            "mapping another model's value tables (e.g. "
            "res_HD,res_UHD,codec_prores). Default: " + ",".join(DEFAULT_ACTION_LABELS)
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="BLE scan timeout in seconds. Default: 15.0",
    )

    return parser.parse_args()


if __name__ == "__main__":
    configure_console_logging()
    raise SystemExit(asyncio.run(run(parse_args())))
